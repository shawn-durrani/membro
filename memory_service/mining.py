"""The reflection pass — mine a conversation's new messages for durable facts.

Every mined fact passes the four walls, plus the per-speaker trust check
(#31: a fact drawn from a guest's or unrecognised speaker's turn is held for
review with the speaker named in the reason); a flag means
written-but-quarantined (low confidence), never silently dropped. The
episodic record is read-only input here — this pass never modifies a message.

Provenance is recorded only where it is real: a fact this pass could not tie
to one turn is stored UNBOUND (`source_message_id` NULL), never pinned to
whichever turn happened to end the window.
"""

import re
import threading
from datetime import datetime, timezone

from . import captions, episodic, ledger, llm, persons, recall, summary, walls

# Per-conversation distill locks. A distill run reads the conversation's
# `mined_upto` watermark once, then mines everything after it. Two runs for the
# SAME conversation overlapping in this process would both read the same
# watermark and re-mine the same messages, multiplying facts (the amplifier in
# #36). We serialize per conversation with a non-blocking guard: if a run is
# already in flight, the second returns immediately — the in-flight run advances
# the watermark and covers those messages. In-process is sufficient: the service
# is a single port-guarded instance and /v1/distill is its only caller.
_distill_locks: dict[str, threading.Lock] = {}
_distill_locks_guard = threading.Lock()


def _conversation_lock(source_app: str, external_id: str) -> threading.Lock:
    key = f"{source_app}\x00{external_id}"
    with _distill_locks_guard:
        return _distill_locks.setdefault(key, threading.Lock())

# A fact line: NEW [supersedes=..] [event=YYYY-MM-DD] [importance=N]: <fact>.
# The three attributes are order-independent and all optional; the fact is
# everything after the first colon. Only lines beginning with NEW are facts —
# anything else is model chatter and is dropped (STRICT).
_HEAD_RE = re.compile(r"^NEW\b(?P<attrs>[^:]*):\s*(?P<fact>.+)$", re.IGNORECASE)
_SUPERSEDES_RE = re.compile(r"supersedes\s*=\s*\[?\s*(\d+(?:\s*,\s*\d+)*)\s*\]?", re.I)
_IMPORTANCE_RE = re.compile(r"importance\s*=\s*(\d{1,2})", re.I)
_EVENT_RE = re.compile(r"event\s*=\s*(\d{4}-\d{2}-\d{2})", re.I)
# The message this fact was derived from, as the [msg N] label the model echoes
# back. It scopes event-date grounding to ONE turn's text (see #33) so a date
# living elsewhere in the chunk can't ground a fact it has nothing to do with.
_SRC_RE = re.compile(r"src\s*=\s*(\d+)", re.I)

# Explicit calendar dates are recognised by `walls.explicit_dates` — ONE
# definition, because the same question ("is this date literally in the text?")
# gates two different things: the event date honoured below, and the temporal
# wall that catches a date invented in a fact's prose (#38). We only ever honour
# a model-supplied event date that is literally anchored in the source — never a
# relative ("last Tuesday") or vague ("a couple weeks ago") phrase, and never a
# date the model invented. Inventing a date is exactly the fabrication this
# project exists to prevent, so grounding is load-bearing, not a nicety.
_explicit_dates = walls.explicit_dates


def _grounded_event_date(model_date: str | None, source_text: str,
                         ref_ts: float) -> float | None:
    """Turn a model-supplied `event=YYYY-MM-DD` into an epoch timestamp — but
    ONLY when that month/day is literally anchored by an explicit calendar date
    in `source_text`. Returns None (→ caller defaults to conversation time) for
    a missing, malformed, or ungrounded date. When the anchoring phrase names no
    year, the year is taken from the conversation (`ref_ts`), never from the
    model, so no part of the date is invented."""
    if not model_date:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", model_date.strip())
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        datetime(year, month, day)
    except ValueError:
        return None
    ref_year = datetime.fromtimestamp(ref_ts, tz=timezone.utc).year
    for mo, dy, yr in _explicit_dates(source_text):
        # Grounded when the day/month match; if the text names a year it must
        # match too, otherwise the model's year is discarded for the ref year.
        if mo == month and dy == day and (yr is None or yr == year):
            final_year = yr if yr is not None else ref_year
            try:
                return datetime(final_year, mo, dy,
                                tzinfo=timezone.utc).timestamp()
            except ValueError:
                return None
    return None


def _retry_importance(fact: str, settings) -> int | None:
    """Reissue ONE fact whose importance= tag the miner omitted or malformed,
    asking explicitly for the missing score before we fall back to quarantine
    (#69). The first-pass omission rate is high enough (the smaller utility
    model drops a required tag under the heavier event=/src= grammar) that
    quarantining on sight would flood the review queue; a single cheap re-ask
    recovers the common case where the fact itself was fine and only the tag
    was dropped. Returns a 1-9 score, or None if the retry ALSO fails to yield a
    usable number — the caller then quarantines as the genuine-second-failure
    backstop. Capped at 9 like the primary path: importance 10 is owner-only
    permanence, never mintable by an automated pass."""
    prompt = (
        f"You are correcting one fact you extracted for a permanent memory "
        f"ledger about {settings.user_name}. You OMITTED its required "
        "importance= tag. Reissue this one fact with an integer importance from "
        "1-9: how much it matters to understanding the person long-term, NOT "
        "how often it came up. Anchors: 1-2 = mundane detail; 4-6 = notable (a "
        "decision, a stated goal, a colleague's role); 8-9 = life-defining (a "
        "new job, a move, family, health). Never assign 10 — it is reserved for "
        "the owner.\n"
        "Output exactly one line: importance=<N>\n\n"
        f"Fact: {fact}"
    )
    out = llm.utility_complete(prompt, settings, max_tokens=50)
    m = _IMPORTANCE_RE.search(out or "")
    return min(9, max(1, int(m.group(1)))) if m else None


# One retry answer line: FACT <k>: src=<N or none>.
_SRC_FIX_RE = re.compile(r"^\s*FACT\s+(\d+)\s*:\s*src\s*=\s*(\d+|none)\s*$",
                         re.I | re.M)


def _retry_src_bindings(unbound: list[tuple[int, str]], source_text: str,
                        settings, turn_ids: set[int]) -> dict[int, int]:
    """Reissue the facts whose src= binding the miner omitted or mis-numbered,
    asking for the [msg N] each one was drawn from (#35).

    Same posture as the #69 importance repair — a corrective retry first,
    quarantine only on a genuine second failure — but batched: ONE call per
    chunk covers every unbound fact, because unlike an importance score the
    binding can't be re-asked without resending the excerpt, and a per-fact
    loop would multiply that cost by every unbound fact in the window. The
    caller only invokes this for guest-present windows, where an unbound fact
    cannot be attributed to the owner and would otherwise burn a review-queue
    row on the generic fail-safe reason (measured before this repair: 1 fact
    in 28 arrived bound, so guest sessions quarantined near-everything).

    `unbound` carries (output_line_index, fact_text) pairs; the return maps
    output_line_index -> msg N for every answer that names a real turn in
    `turn_ids`. A src=none answer (honest cross-turn synthesis), a turn number
    the window doesn't contain, or missing/chatter output simply leaves that
    fact out of the map — the caller's existing fail-safe hold is the
    unchanged backstop, never silent trust. Naming a real turn is necessary
    but not sufficient: the caller checks every answer here against the turn
    it names (`_repair_binding_refused`) before letting it grant trust."""
    listing = "\n".join(f"FACT {k}: {fact}"
                        for k, (_, fact) in enumerate(unbound, start=1))
    prompt = (
        f"You are correcting facts you extracted for a permanent memory ledger "
        f"about {settings.user_name}. The excerpt below has multiple human "
        "speakers, so every fact MUST name its source turn, and you omitted or "
        "mis-numbered the required src= tag on the facts listed here.\n"
        "For each fact, answer with the [msg N] label of the SINGLE turn it "
        "was drawn from — copy the label off that turn, do not count. If a "
        "few adjacent turns contribute, cite the one that states the fact "
        "most directly. Only if no single turn states it (pure cross-turn "
        "synthesis) answer src=none — never guess.\n"
        "Output exactly one line per fact, nothing else:\n"
        "FACT <k>: src=<N or none>\n\n"
        f"## Facts needing their source turn\n{listing}\n\n"
        f"## Conversation excerpt\n{source_text}"
    )
    out = llm.utility_complete(prompt, settings,
                               max_tokens=min(400, 60 + 15 * len(unbound)))
    repairs: dict[int, int] = {}
    for m in _SRC_FIX_RE.finditer(out or ""):
        k, val = int(m.group(1)), m.group(2).lower()
        if 1 <= k <= len(unbound) and val != "none" and int(val) in turn_ids:
            repairs[unbound[k - 1][0]] = int(val)
    return repairs


def _repair_binding_refused(fact: str, cand: dict, new_msgs: list[dict],
                            allow: set[str]) -> str | None:
    """Check a src= binding the miner supplied on the #35 corrective RETRY
    against the turn it names. None = honour the binding; a short phrase = the
    reason it was refused (the fact then stays unbound and the existing
    fail-safe holds it).

    Why only the retry's bindings: the first pass answers with the whole
    excerpt in front of it, and its bindings are judged exactly as they always
    have been. The retry is a second, narrower guess made after the miner
    already failed to point at a source once — and in a guest-present window
    that guess decides ATTRIBUTION. A retry that answers with the owner's turn
    for a sentence a guest actually said converts guest speech into canon at
    high confidence, and nothing downstream would notice: the grounding wall
    reads the whole chunk (a guest's words ARE in the chunk), and the speaker
    trust flag reads the bound turn, which is now the owner's.

    So the binding has to survive two deterministic checks, both fail-safe:

      1. the named turn must share at least one distinctive content word with
         the fact — a turn that has none of its wording did not state it;
      2. no OTHER speaker's turn in the window whose speech is untrusted (a
         guest, an unidentified voice, an unrecognised class) may match the
         fact's wording as well as, or better than, the named turn. A tie is
         refused on purpose: "these words look at least as much like Sam's" is
         precisely the case where attributing them to the owner is a guess.

    Refusing costs a review-queue row; accepting a wrong binding costs a false
    fact about the owner that reads as if he said it. This is the direction
    that error should fall.
    """
    if cand is None:     # belt and braces: the caller already filters to real
        return "it named no turn in this window"   # turns in this window
    support = walls.lexical_support(fact, _msg_body(cand), allow)
    if support == 0:
        return "the turn it named shares none of this fact's wording"
    rival = max((walls.lexical_support(fact, _msg_body(m), allow)
                 for m in new_msgs
                 if m["id"] != cand["id"] and walls.speaker_trust_flag(m["speaker"])),
                default=0)
    if rival >= support:
        return ("another speaker's turn in this window matches this fact's "
                "wording at least as closely as the turn it named")
    return None

# A long-idle conversation (e.g. an imported backlog) can hold more transcript
# than the utility model's context window — mine in bounded windows, advancing
# the watermark after each so an interrupted run resumes where it stopped.
# Both bounds matter: count for ordinary chat, characters for pasted walls of
# text (one 200k-char YouTube transcript busts a count-only window).
CHUNK_MSGS = 120
CHUNK_CHARS = 350_000   # ~85k tokens of transcript per window
MSG_CHARS = 20_000      # a single pasted document is truncated for mining —
                        # the episodic record keeps it verbatim; only this
                        # pass reads a bounded view

# #71: the supersede-candidate list used to be ONLY the 250 newest valid facts
# by insertion id (ledger.list_facts orders id DESC) — a structural blind
# spot, not a tuning knob. A fact contradicted by new information but never
# re-mentioned since simply aged out of the window and became permanently
# unreachable for supersession, however directly a later fact contradicted
# it (measured on a dense ledger: ~90%). Widening the window only moves the
# cliff, so instead we ADD a second, topic-scoped source of candidates:
# recall.recall() ranked by relevance to THIS chunk, independent of id.
# That reaches an old, on-topic fact regardless of how long ago it was
# inserted. It degrades gracefully exactly like every other recall call —
# no embeddings key means keyword-only matching, not a hard failure — so
# keyless test/CI runs still exercise real (if coarser) reach.
SEMANTIC_SUPERSEDE_CANDIDATES = 40
SEMANTIC_QUERY_CHARS = 4000     # gist, not the whole chunk — the embeddings
                                # endpoint has its own input-length ceiling


def _msg_body(m: dict) -> str:
    """The bounded text view of ONE message for mining: its content plus any
    attachment text, truncated to MSG_CHARS. Attachment text mines alongside the
    message it traveled with — a pasted document's facts are as real as typed
    ones. The episodic record keeps every byte; this pass reads a bounded view.
    This is also the text a fact's event date is grounded against once the fact
    is bound to its source message (#33)."""
    c = m["content"]
    for a in m.get("attachments") or []:
        if a.get("extracted_text"):
            c += f"\n[Attached file: {a['filename']}]\n{a['extracted_text']}"
    return c if len(c) <= MSG_CHARS else c[:MSG_CHARS] + " … [truncated]"


def _speaker(m: dict, user_name: str) -> str:
    """The transcript label for one turn. The owner's turns carry their real
    name; a guest's turns carry the guest's name plus a "(guest)" marker so
    the extractor can attribute speech per speaker (#31); an unidentified
    human turn is labelled as exactly that, never defaulted to the owner.
    Model slugs and unrecognised values render verbatim."""
    s = m["speaker"]
    if s == "user":
        return user_name
    cls = walls.speaker_class(s)
    if cls == "guest":
        return f"{walls.guest_name(s)} (guest)"
    if cls == "guest-unknown":
        return "Unidentified speaker (guest)"
    return s


def _transcript(messages: list[dict], user_name: str) -> str:
    """The chunk rendered as prompt text, each turn prefixed with a stable
    [msg N] label (N = 1-based position in this window). The label is what lets
    the extractor attribute each fact back to the single turn it came from: the
    model echoes it as src=N, and that binding is what scopes event-date
    grounding to one message instead of the whole chunk (#33)."""
    return "\n\n".join(
        f"[msg {i}] {_speaker(m, user_name)}: {_msg_body(m)}"
        for i, m in enumerate(messages, start=1))


def _view_len(m: dict) -> int:
    n = len(m["content"]) + sum(len(a.get("extracted_text") or "")
                                for a in m.get("attachments") or [])
    return min(n, MSG_CHARS)


def _next_chunk(msgs: list[dict]) -> list[dict]:
    """Up to CHUNK_MSGS messages within a CHUNK_CHARS transcript budget
    (counting each message's truncated view incl. attachment text); always
    at least one."""
    chunk, chars = [], 0
    for m in msgs[:CHUNK_MSGS]:
        chars += _view_len(m)
        if chunk and chars > CHUNK_CHARS:
            break
        chunk.append(m)
    return chunk


def distill(con, settings, source_app: str, conversation_external_id: str,
            regenerate: bool = True) -> dict:
    lock = _conversation_lock(source_app, conversation_external_id)
    if not lock.acquire(blocking=False):
        # Another distill for THIS conversation is already running in-process;
        # letting this one proceed would re-mine the same messages and multiply
        # facts (#36). Skip — the in-flight run covers these messages.
        return {"added": 0, "quarantined": 0, "skipped_locked": True}
    try:
        conv = episodic.get_conversation(con, source_app, conversation_external_id)
        if not conv:
            raise LookupError("unknown conversation")
        added = quarantined = deduped = refused = deferred = 0
        # Images first (#107): captions land in extracted_text, which the
        # attachment view below reads — so photo content mines exactly like a
        # pasted document's. Any caption failure skips that image and never
        # blocks the text mining that follows.
        captions.caption_pending(con, settings, conv["id"])
        att_by_msg = episodic.attachments_for_conversation(con, conv["id"])
        while True:
            pending = episodic.messages_after(con, conv["id"], conv["mined_upto"])
            for m in pending:
                m["attachments"] = att_by_msg.get(m["external_id"], [])
            new_msgs = _next_chunk(pending)
            if not new_msgs:
                break
            chunk = _distill_chunk(con, settings, source_app, conv, new_msgs)
            added += chunk["added"]
            quarantined += chunk["quarantined"]
            deduped += chunk.get("deduped", 0)
            refused += chunk.get("refused_supersede", 0)
            deferred += chunk.get("deferred_supersede", 0)
            conv["mined_upto"] = new_msgs[-1]["id"]
        if added and regenerate:
            summary.regenerate(con, settings)
        result = {"added": added, "quarantined": quarantined}
        if deduped:
            # Only surfaced when a re-mine was actually collapsed (#36) — keeps
            # the common return shape unchanged for existing callers/tests.
            result["deduped"] = deduped
        if refused:
            # Same shape rule as deduped: present only when a backdated
            # supersession was actually refused (#120).
            result["refused_supersede"] = refused
        if deferred:
            result["deferred_supersede"] = deferred
        return result
    finally:
        lock.release()


# Speaker classes that count as HUMAN SPEECH worth mining (#31). Guest turns
# mine even with the owner silent in the window — their facts land quarantined,
# so nothing gains trust — but a window of only model turns and/or unrecognised
# speaker classes skips the LLM exactly as model-only windows always have: an
# unrecognised class is not known to be a human voice at all (it could be a
# tool or a transcript artefact), so mining it as biography would guess.
_HUMAN_SPEAKERS = ("owner", "guest", "guest-unknown")


def _distill_chunk(con, settings, source_app: str, conv: dict,
                   new_msgs: list[dict]) -> dict:
    if not any(walls.speaker_class(m["speaker"]) in _HUMAN_SPEAKERS
               for m in new_msgs):
        _advance(con, conv["id"], new_msgs[-1]["id"])
        return {"added": 0, "quarantined": 0, "deduped": 0,
                "refused_supersede": 0, "deferred_supersede": 0}

    # #31: does this window carry any speech that is NOT the owner's or a model
    # seat's? If so, a fact with no valid src= binding cannot be attributed to
    # the owner, and the fail-safe below holds it for review.
    untrusted_speaker_present = any(
        walls.speaker_trust_flag(m["speaker"]) for m in new_msgs)

    source_text = _transcript(new_msgs, settings.user_name)
    recent = ledger.list_facts(con, status="valid", limit=250)
    candidates = {f["id"]: f for f in recent}
    try:
        # Reach beyond the recency window (#71): facts relevant to THIS
        # chunk's topics, however old, so a directly contradicted fact is
        # still offered as a supersede target. Merge, don't replace — the
        # recency window still gives the model everything freshly discussed,
        # which the semantic pass isn't guaranteed to surface.
        for f in recall.recall(con, settings, query=source_text[:SEMANTIC_QUERY_CHARS],
                               limit=SEMANTIC_SUPERSEDE_CANDIDATES):
            candidates.setdefault(f["id"], f)
    except Exception:
        pass  # semantic reach degrades; the recency window above still works
    recent = sorted(candidates.values(), key=lambda f: f["id"], reverse=True)
    recent_text = "\n".join(f"- [{f['id']}] {f['content']}" for f in recent) or "(empty)"
    # The [msg N] labels in source_text map back to real messages here; a fact's
    # src=N is validated against this before it can narrow event-date grounding.
    idx_to_msg = {i: m for i, m in enumerate(new_msgs, start=1)}
    # Guest guidance enters the prompt only when the window actually carries
    # guest (or unrecognised) speech, so the everyday owner-only path pays
    # nothing and its prompt is byte-identical to before (#31).
    guest_rules = "" if not untrusted_speaker_present else (
        "GUEST SPEAKERS: turns labelled \"(guest)\" are OTHER HUMANS in the "
        f"session — never {settings.user_name} and never an AI participant. "
        "Resolve pronouns PER SPEAKER: a guest's 'I' is that guest, not "
        f"{settings.user_name}. Phrase a guest's fact in third person, naming "
        "the guest (e.g. a guest called Sam saying 'I hate coriander' becomes "
        f"'{settings.user_name}'s wife Sam dislikes coriander' when the "
        "excerpt states that relationship, else 'Sam (a guest) dislikes "
        f"coriander') — NEVER as a first-person fact about "
        f"{settings.user_name}. Do not build a profile of a guest: extract a "
        "guest's statement only when it matters to understanding "
        f"{settings.user_name}'s world (family, close relationships, shared "
        "plans, things that affect them). Speech from an unidentified speaker "
        "may still be extracted, attributed to 'an unidentified guest'. Every "
        "fact drawn from a guest's turn must carry src=<N> naming that "
        "guest's own turn.\n")
    prompt = (
        f"You maintain a permanent memory ledger about {settings.user_name}. From the "
        "conversation excerpt below, extract durable facts worth remembering long-term. "
        "Capture ALL dimensions, not only the technical ones:\n"
        "- identity, work, projects, preferences, decisions, skills\n"
        "- PEOPLE & RELATIONSHIPS: who matters to them and durable facts about those "
        "people (family, friends, colleagues — names, ages, needs, roles). Not name-drops.\n"
        "- GOALS & DIRECTION: what they're working toward or worried about over time. "
        "Not passing moods.\n"
        "The test is DURABILITY (still true in ~6 months), not topic — keep durable "
        "relationships and goals; drop transient feelings and chit-chat.\n"
        "PROJECT / BUILDER CONVERSATIONS (#66) get a stricter two-level rule, "
        "because this ledger is biography, not an engineering log — the full "
        "transcript already exists, searchable, for anyone who needs the technical "
        "detail:\n"
        "  1. You may extract AT MOST ONE concise active-thread/outcome fact per "
        "project per excerpt: what they're working on and why it matters (e.g. "
        f"'{settings.user_name} is working on reducing Larkspur's request-cache "
        "cost'), OR a shipped/meaningful outcome (launched, shipped, picked an "
        "architecture, hit a real milestone).\n"
        "  2. NEVER extract the technical reasoning, diagnosis, root-cause "
        "analysis, implementation options, error messages, or step-by-step "
        "mechanics themselves — regardless of how detailed or important they "
        "sound, and even when they don't match any 'process' keyword list (e.g. "
        "'the response cache keys on an exact header match', 'PR 42 passed "
        "the full test suite keyless', 'latency root-caused to a missing index'). "
        "That is reasoning about the project, not a fact about the person — it "
        "already lives in the read-only conversation record. Judge this by "
        "SUBSTANCE (what/why about the person vs. how/why-technically about the "
        "system), never by whether the sentence happens to contain PR/CI/build "
        "vocabulary.\n"
        "DO NOT extract: anything about the AI participants or about this memory "
        "system itself (its build state, code, operations); biography from "
        "roleplay, persona, or interview-prep framing, or from an assistant's own "
        "'what do you know about me' summary; or anything already in the existing "
        "entries.\n"
        f"{guest_rules}"
        "Each turn in the excerpt is prefixed with a [msg N] label. Every fact must "
        "carry src=<N> naming the SINGLE message it was drawn from — copy the label "
        "off that turn, do not count. If a fact draws on a few adjacent turns, cite "
        "the one that states the fact most directly (and, if dating, the one that "
        "states the date).\n"
        "Output one fact per line in exactly one of these formats:\n"
        "NEW src=<N> importance=<1-10>: <fact as a plain sentence>\n"
        "NEW src=<N> supersedes=<id> importance=<1-10>: <fact that replaces/contradicts "
        "existing entry <id>>\n"
        "(use supersedes when the new fact updates or contradicts a listed entry — the "
        "old entry is kept as history, not deleted)\n"
        "DATING: by default a fact is dated to this conversation. If — and only if — "
        "the excerpt states an EXPLICIT CALENDAR DATE for when the fact's event "
        "actually happened (e.g. 'June 30', 'on the 13th of July', '2026-07-11'), add "
        "event=<YYYY-MM-DD> so the fact is dated to that day — and make sure src=<N> "
        "points to the very message that states that date, because the date is only "
        "honoured when it literally appears in that one message:\n"
        "NEW src=<N> event=<YYYY-MM-DD> importance=<1-10>: <fact>\n"
        "NEVER use event= for relative or vague times ('last week', 'a couple weeks "
        "ago', 'recently'), and NEVER guess or infer a date. If no explicit calendar "
        "date is written in the excerpt, omit event= entirely.\n"
        "The same rule governs the fact's own WORDING: never translate a relative "
        "time into a calendar date, and never add a gloss the excerpt doesn't state "
        "(no '(tomorrow)', no 'next week', no working out which Saturday). Keep the "
        "source's own qualifier verbatim — 'on Saturday, about nine days away' stays "
        "exactly that. A reader can resolve it against the fact's date; a wrong "
        "resolution is permanent.\n"
        "importance is how much this fact matters to understanding the person "
        "long-term, NOT how often it came up. Anchors: 1-2 = mundane detail (a tool "
        "preference, a one-off errand); 4-6 = notable (a project decision, a stated "
        "goal, a colleague's role); 8-9 = life-defining (a new job, a move, family, "
        "health). Most facts are 3-6; reserve 8-9 for genuinely major items. Never "
        "assign 10 — it is reserved for the owner to mark facts as permanent.\n"
        "If there is nothing new, output exactly: NONE\n\n"
        f"## Existing valid entries (with ids — do not repeat)\n{recent_text}\n\n"
        f"## Conversation excerpt\n{source_text}"
    )
    out = llm.utility_complete(prompt, settings, max_tokens=1000)
    valid_ids = {f["id"] for f in recent}
    allow = ({w.lower() for w in settings.grounding_allowlist}
             | {settings.user_name.lower()}
             | persons.grounding_names(con))  # #57: names membro already holds
    # #35: in a guest-present window an unbound fact cannot be attributed to
    # the owner, so before judging anything, give the miner ONE batched
    # corrective retry for every fact whose src= is missing or names no real
    # turn — the same repair-then-backstop posture as the #69 importance
    # retry. Owner-only windows never reach this (unbound is fine there), so
    # the everyday path pays nothing. The pre-scan mirrors the main loop's
    # parsing exactly, minus the drop-counter side effects: a line the main
    # loop will drop or skip is not worth a repair slot.
    src_repairs: dict[int, int] = {}
    if untrusted_speaker_present:
        unbound: list[tuple[int, str]] = []
        for i, raw in enumerate(out.splitlines()):
            raw = raw.strip().lstrip("-• ").strip()
            if not raw or raw.upper() == "NONE":
                continue
            h = _HEAD_RE.match(raw)
            if not h:
                continue
            fact = h.group("fact").strip()
            if (len(fact) < 8 or walls.is_system_meta(fact)
                    or walls.is_builder_process(fact)):
                continue
            src = _SRC_RE.search(h.group("attrs"))
            if src is None or int(src.group(1)) not in idx_to_msg:
                unbound.append((i, fact))
        if unbound:
            src_repairs = _retry_src_bindings(
                unbound, source_text, settings, set(idx_to_msg))
    added = quarantined = deduped = refused = deferred = 0
    for line_no, line in enumerate(out.splitlines()):
        line = line.strip().lstrip("-• ").strip()
        if not line or line.upper() == "NONE":
            continue
        m = _HEAD_RE.match(line)
        if not m:
            continue  # STRICT: non-NEW lines are model chatter, never facts
        fact = m.group("fact").strip()
        if len(fact) < 8:
            continue
        if walls.is_system_meta(fact):
            # Self-reference to this memory system's machinery or the AI tooling
            # itself — not biography about the user, so it is never extracted
            # (dropping keeps the review queue from flooding when a conversation
            # is ABOUT the memory system — #36). The verbatim message survives in
            # the episodic record, so nothing is actually lost.
            walls.record_drop("system-meta")
            continue
        if walls.is_builder_process(fact):
            # Ordinary engineering-process/execution chatter about building a
            # project — PR/issue/CI mechanics, implementation minutiae, transient
            # UI styling (#66). Same reasoning as is_system_meta: not biography,
            # nothing for a human to review, and the verbatim message still
            # survives in the episodic record.
            #
            # This is a keyword-matching BACKSTOP, not the primary defense — the
            # live-evidence gap in #66's reopening showed deep technical/
            # architecture reasoning (cache-invalidation analysis, root-causing)
            # trips no keyword here at all. The real fix is upstream, in the
            # extraction prompt itself: the model is asked not to propose that
            # content as a fact in the first place, because separating "outcome
            # fact about the person" from "technical reasoning about the system"
            # is a judgment call regex can't make reliably.
            walls.record_drop("builder-process")
            continue
        attrs = m.group("attrs")
        supersedes = _SUPERSEDES_RE.search(attrs)
        imp = _IMPORTANCE_RE.search(attrs)
        event = _EVENT_RE.search(attrs)
        src = _SRC_RE.search(attrs)
        # Miner is capped at 9: importance 10 means PERMANENT (never decays,
        # always in the profile) and only the owner may grant that — a batch
        # scoring hiccup must never be able to mint permanence.
        #
        # importance is REQUIRED, never optional (#69). After the output grammar
        # gained event= and src= tags (2026-07-15), the utility model began
        # dropping the importance= tag under the heavier load, and this parser
        # accepted the omission and stored NULL. Summary selection reads NULL as
        # a neutral 5 (weighting.DEFAULT_IMPORTANCE), so genuinely important,
        # recent facts silently lost their place in the profile to older facts
        # carrying explicit high scores. We no longer trust an unscored fact —
        # but we don't quarantine on sight either: a missing OR malformed
        # importance= (the regex won't match a non-numeric value) first triggers
        # ONE corrective re-ask for this fact's score (_retry_importance). Only a
        # genuine SECOND failure falls through to the quarantine flag below,
        # handled by the same wall machinery — the fact is then written and HELD
        # FOR REVIEW (append-only, "gate the write, don't verify the writer"),
        # never silently stored as canon with an invisible guessed score. The
        # retry keeps the high first-pass omission rate from flooding review.
        importance = min(9, max(1, int(imp.group(1)))) if imp else None
        if importance is None:
            importance = _retry_importance(fact, settings)
        # A valid src=N binding is a HARD PREREQUISITE for honouring a
        # model-supplied event date. With one, we ground the date against that
        # one turn's text (and record it as the source message). Without one —
        # src missing, OR naming no real turn — the model has failed to point at
        # a source, so honouring any date it proposed is pure guessing: we reject
        # the event date outright and fall back to conversation time. We never
        # ground against the latest turn's text as a consolation, because a date
        # that happens to sit in that turn could still borrow onto an unrelated
        # fact — a narrower re-opening of the exact #33 hole.
        bound = idx_to_msg.get(int(src.group(1))) if src else None
        repair_refused = None
        if bound is None and line_no in src_repairs:
            # #35: the corrective retry supplied the binding the first pass
            # omitted or mis-numbered — but only after it survives
            # _repair_binding_refused does the fact behave as if it had
            # arrived bound (speaker trust, event-date grounding, and the
            # recorded source message all read from this turn). A refused
            # repair leaves the fact unbound, exactly as if the retry had
            # answered src=none, so the fail-safe below still holds it.
            cand = idx_to_msg.get(src_repairs[line_no])
            repair_refused = _repair_binding_refused(fact, cand, new_msgs, allow)
            if repair_refused is None:
                bound = cand
        if bound is not None:
            conv_ts = bound["created_at"]
            event_date = _grounded_event_date(
                event.group(1) if event else None,
                _msg_body(bound), conv_ts) or conv_ts
            source_message_id = bound["id"]
        else:
            conv_ts = new_msgs[-1]["created_at"]
            event_date = conv_ts            # no binding → conversation time, full stop
            # ...and NO source message. The window's last turn used to be
            # recorded here as a stand-in, which read downstream (the admin
            # page's "source message #N", any reviewer following the trail) as
            # real provenance for a fact that was never tied to that turn — or
            # to any turn. An unbound fact is stored unbound: the transcript
            # still holds every word, and `conversation_id` still says which
            # chat it came from, so nothing is lost except a claim we cannot
            # support.
            source_message_id = None
        # The walls (grounding/source-trust/scope) still read the whole chunk:
        # a fact may legitimately synthesise across turns, and #33 is about the
        # event date only — narrowing the walls here would be scope creep.
        flags = walls.check(fact, source_text, allow)
        # #31: source-trust for WHO SPOKE. A fact bound to a guest's turn, an
        # unidentified speaker's, or an unrecognised speaker class's is held
        # for review with the speaker named in the reason — the same posture
        # `mcp:*` writes get from the ledger gate. And when such turns exist in
        # the window but the fact still carries no valid binding after the #35
        # corrective retry, it cannot be attributed to the owner, so it is held
        # too (fail safe — attribution is exactly what this window can no
        # longer assume, and the retry was its second chance). Owner-only
        # windows are unchanged: an unbound fact there behaves as it always
        # has, because every human turn is the owner's.
        if bound is not None:
            speaker_flag = walls.speaker_trust_flag(bound["speaker"])
        elif not untrusted_speaker_present:
            speaker_flag = None
        elif repair_refused:
            # The retry did answer, and the answer did not hold up against the
            # turn it named. Same fail-safe hold, but the reason says which of
            # the two failures happened, so the reviewer knows whether the
            # miner never pointed anywhere or pointed somewhere unsupported.
            speaker_flag = (
                "speaker-trust: the src= binding supplied on retry does not "
                f"hold up — {repair_refused} — so this fact cannot be "
                "attributed to the owner")
        else:
            speaker_flag = (
                "speaker-trust: no valid src= binding, and none supplied on "
                "retry, in a session with guest or unrecognised speakers — "
                "cannot attribute this fact to the owner")
        if speaker_flag:
            flags = flags + [speaker_flag]
        if importance is None:
            # #69: still unscored AFTER the corrective retry — the backstop.
            # Quarantine, don't silently trust.
            flags = flags + [
                "importance: miner omitted or malformed importance= and did "
                "not supply it on retry — unscored, score it before trusting"]
        res = ledger.add_fact(
            con, fact, settings, source="chat", origin_agent=source_app,
            source_app=source_app, conversation_id=conv["id"],
            source_message_id=source_message_id,
            event_date=event_date,
            confidence="low" if flags else "high",
            importance=importance,
            quarantine_reason=("; ".join(flags) + " — review before trusting")
            if flags else None,
            dedupe_in_conversation=True)
        if res.get("duplicate"):
            # Already mined from this conversation on an earlier pass (e.g. a run
            # that added facts then died before advancing the watermark, so this
            # chunk was re-mined). Not a new fact — do not count or re-supersede.
            deduped += 1
            continue
        added += 1
        if res["quarantined"]:
            quarantined += 1
        if supersedes:
            for old in supersedes.group(1).split(","):
                old_id = int(old.strip())
                if old_id not in valid_ids:
                    continue
                if res["quarantined"]:
                    # A held-for-review fact must not alter canon (#120): its
                    # supersession intent is deferred to the human review pass
                    # (the queue's supersede action), never auto-applied from
                    # quarantine.
                    deferred += 1
                    continue
                # An older claim never invalidates a newer one (#120). When
                # mining history (imported exports, late re-mines), this fact's
                # grounded event date can precede the listed target's — the
                # fact itself still lands above, event-dated and walled; only
                # the invalidation is refused. Day granularity, matching
                # day-grounded event dates, so same-day corrections pass.
                row = con.execute("SELECT event_date FROM facts WHERE id=?",
                                  (old_id,)).fetchone()
                if row and row[0] and row[0] - event_date > 86400:
                    refused += 1
                    continue
                ledger.mark_superseded(con, old_id, res["id"])
    _advance(con, conv["id"], new_msgs[-1]["id"])
    return {"added": added, "quarantined": quarantined, "deduped": deduped,
            "refused_supersede": refused, "deferred_supersede": deferred}


def _advance(con, conversation_id: int, upto: int) -> None:
    con.execute("UPDATE conversations SET mined_upto=? WHERE id=?",
                (upto, conversation_id))
    con.commit()
