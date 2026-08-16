"""The extraction walls — deterministic checks applied to every mined fact.

Three of them QUARANTINE (grounding, temporal grounding, source-trust): a
flagged fact is written but held for human review — never silently dropped,
never silently trusted. (See docs/MEMORY_INTEGRITY.md for the worked
contamination case.)

The third, `is_system_meta`, is a DROP filter, not a quarantine: a "fact" that
is about this memory system's own machinery or the AI tooling itself is not
biography about the user at all, so there is nothing for a human to approve —
holding it for review only floods the queue when a conversation happens to be
ABOUT the memory system (the #36 failure mode). Such lines are never extracted.
It is deliberately narrow — it matches system/AI-tooling *mechanics* vocabulary,
never the generic words ("deployed", "the system", "committed") or product names
a genuine career/project fact might legitimately carry.

`is_builder_process` (#66) is a sibling DROP filter, same discipline, for a
different flood: ordinary engineering/execution chatter about ANY project being
built — PR/issue mechanics, code review and CI status, and transient UI
styling/layout minutiae — none of which is durable biography about the user
either, even when the conversation isn't about this memory system at all. Same
"near-zero false positive" bar as `is_system_meta`: it matches process/mechanics
vocabulary, never the generic verbs or product names a real career/project
decision or preference would use, so a fact like "Alex shipped the v2 redesign"
or "Alex prefers dark-mode UIs" is judged by the quarantine walls like anything
else, never silently dropped here.

`speaker_class` / `speaker_trust_flag` (#31) extend the source-trust posture
to WHO SPOKE. Ingest may attribute turns to other humans in the room
(`guest:<name>`, or `guest:unknown` when diarization could not identify the
voice confidently). A fact the miner draws from such a turn is quarantined by
default, with the speaker named in the stated reason: the same held-for-review
treatment `mcp:*` writes get from the ledger gate, never a silent drop and
never silent trust. Any speaker class this build does not recognise is treated
the same way (fail safe), so a newer client can never mint trust by inventing
a class. The owner's own `user` turns and the model seats are unchanged.
These checks live here beside the other walls; the mining pass supplies the
per-fact speaker binding, so `check()` below is unchanged.

`lexical_support` is not a wall and never quarantines anything by itself. It
is the deterministic word-overlap primitive the mining pass uses to check a
src= binding the miner supplied on a CORRECTIVE RETRY, so an unverifiable
guess about who spoke cannot quietly turn a guest's words into the owner's
canon. It only ever fails toward the existing hold.
"""

import re
import threading
from collections import Counter
from datetime import datetime

_CAP_RE = re.compile(r"[A-Z][\w&.+-]*(?:\s+[A-Z][\w&.+-]*)*")

# words the capital-letter regex over-captures; NOT user-specific (that list is
# config: Settings.grounding_allowlist — personal nouns never live in code)
_STOPWORDS = {
    "i", "the", "a", "an", "he", "she", "his", "her", "him", "new", "none", "it",
    "its", "they", "and", "but", "also", "with", "for", "this", "that",
    "ai", "api", "url", "urls", "ui", "ux", "ceo", "cto", "mcp", "llm", "rag",
    "ide", "sdk", "os", "tv", "id", "ok",
}

_PERSONA_RE = re.compile(
    r"\b(context for the model|candidate context|pretend (you|to be)|role-?play|"
    r"rehears\w*|persona|you are playing|mock interview|what do you (know|remember) "
    r"about me|tell me about myself|my profile|keep it pg)\b", re.I)

# System/AI-tooling SELF-REFERENCE — the machinery this service is built from,
# and the operation of the AI tools in the chat. Vocabulary a fact about the
# USER essentially never contains. A match means the line is meta-chatter, not
# biography, and is DROPPED (see is_system_meta), not quarantined.
#
# Intentionally scoped to mechanics terms. Removed from the old scope regex:
# generic verbs/nouns that appear in real user facts — "deployed", "committed
# and push", "this app", "the system", "backfill", "memory service" — so a
# career/project fact that merely mentions a product or a deploy is NOT dropped
# and is judged by the grounding/source-trust walls like anything else (#36).
#
# The set is kept to terms with a near-zero false-positive rate on real
# biography. Words that are common English or plausible in a work fact are
# deliberately EXCLUDED (e.g. "distill", a plain verb; "review queue"/"the
# walls", which a developer might use about their own job) — those stay eligible
# and are judged by the quarantine walls, so at worst they land in review, never
# silently dropped.
_SYSTEM_META_RE = re.compile(
    r"\b(claude code|memory ledger|grounding wall|scope wall|source-trust wall|"
    r"the extraction walls?|quarantin\w*|supersed\w*|the miner|mined?_upto|"
    r"consolidation (pass|sweep)|synthesis fact|PID \d)\b", re.I)

# Builder-process / execution-chatter DROP vocabulary (#66) — engineering
# mechanics for ANY project being built, not specific to this memory system.
# Same discipline as _SYSTEM_META_RE: near-zero false-positive rate on real
# biography. Four clusters, each keyed to the issue's own examples:
#   - PR/issue/ticket handling: "issue #12", "PR #12", "pull request", opening/
#     closing/merging one.
#   - review/CI chatter: code review, LGTM, merge conflict, rebase/cherry-pick,
#     a build or test suite going green/red/flaky.
#   - implementation minutiae: compiler/runtime-error vocabulary a real project
#     OUTCOME never uses ("Alex shipped X" is fine; "hit a null pointer" isn't).
#   - transient UI styling/layout: CSS-property-level detail, never bare "UI"
#     or "design" (those can carry a real preference or outcome).
# Deliberately excluded: bare "refactor\w*", "deploy\w*", "review", "merge",
# "test\w*", "design", "UI/UX" — all plausible in a genuine project fact
# ("Alex redesigned the onboarding flow", "the team merged with another org").
_BUILDER_PROCESS_RE = re.compile(
    r"\b("
    # PR / issue / ticket mechanics
    r"(?:issue|pr|pull request|ticket)\s*#\d+|#\d+\s*(?:issue|pr|pull request|ticket)|"
    r"pull request|opened (?:a|the) (?:pr|pull request|issue)|"
    r"(?:merged|closed) (?:the |a )?(?:pr|pull request|issue|ticket)|"
    # review / CI / merge-mechanics chatter
    r"code review|review comment|LGTM|merge conflict|"
    r"squash(?:ed|ing)?(?: and)? merged?|rebas\w*|cherry-pick\w*|"
    r"CI (?:is |just )?(?:green|red|passing|failing|broke\w*)|"
    r"(?:green|red) build|flaky test\w*|hotfix\w*|"
    # implementation minutiae (compiler/runtime-error vocabulary)
    r"stack trace|null pointer|off-by-one|compile error|syntax error|"
    r"type error|boilerplate code|"
    # transient UI styling/layout minutiae
    r"button color|border-radius|z-index|pixel[- ]perfect|css class|"
    r"styling pass|layout tweak|spacing tweak|ui polish"
    r")\b", re.I)

# Aggregate DIAGNOSTICS only — an in-process, in-memory count of drops by
# reason, never a record of WHAT was dropped (that content is never stored
# anywhere; it lives only in the read-only episodic transcript, same as any
# other line the miner declined to extract). Resets on restart; deliberately
# not persisted to disk and not part of the versioned API contract — it's a
# cheap operational signal ("is the builder-process filter firing a lot?"),
# not an audit trail, and never surfaces in the user's review queue.
_drop_counts_lock = threading.Lock()
_DROP_COUNTS: Counter = Counter()


def record_drop(reason: str) -> None:
    """Bump the in-memory counter for a drop reason ('system-meta' /
    'builder-process'). Never stores the dropped content itself."""
    with _drop_counts_lock:
        _DROP_COUNTS[reason] += 1


def drop_diagnostics() -> dict[str, int]:
    """A snapshot of drop counts by reason since this process started."""
    with _drop_counts_lock:
        return dict(_DROP_COUNTS)


# A capture position counts as a sentence start at the beginning of the
# text or after terminal punctuation. Used by proper_nouns (#57): the miner
# often opens a fact with a capitalised ordinary word ("Achieving...",
# "Proposed fix..."), which is capitalisation-by-position, not a name.
_SENT_START_RE = re.compile(r"(?:^|[.!?:;]\s+|\n\s*)[\"'(]*$")


def _dehyphen(s: str) -> str:
    return s.replace("-", "").replace("–", "")


def proper_nouns(text: str, allowlist: set[str]) -> list[str]:
    text = text or ""
    out = []
    for m in _CAP_RE.finditer(text):
        ph = m.group(0).strip().strip(".,;:!?'\"")
        low = ph.lower()
        if not ph or low in _STOPWORDS or low in allowlist:
            continue
        # multi-word capture made only of stopwords/allowed words ("The API")
        if all(t in _STOPWORDS or t in allowlist for t in low.split()):
            continue
        # #57: a single word capitalised only because it starts a sentence is
        # not an entity. It stays one when the same capitalised word recurs
        # elsewhere in the fact (a real name used as an opener does).
        if (" " not in ph and _SENT_START_RE.search(text[:m.start()])
                and len(re.findall(rf"\b{re.escape(ph)}\b", text)) == 1):
            continue
        out.append(ph)
    return out


def _token_supported(low_tok: str, orig_tok: str, src: str) -> bool:
    """One entity token supports grounding when found in the source. Tokens of
    4+ characters keep the original loose substring match. Shorter tokens
    qualify only when they look like hardware/acronym vocabulary (contain a
    digit, or were written in capitals) and match on a word boundary (#57) —
    'gb' must not ground off 'rugby'."""
    if len(low_tok) >= 4:
        return low_tok in src
    if len(low_tok) >= 2 and (any(c.isdigit() for c in low_tok) or orig_tok.isupper()):
        return re.search(rf"\b{re.escape(low_tok)}\b", src) is not None
    return False


def ungrounded_entities(fact: str, source_text: str, allowlist: set[str]) -> list[str]:
    """Proper nouns in `fact` with no support in `source_text`. Empty = grounded."""
    src = (source_text or "").lower()
    src_dehyphened = _dehyphen(src)
    missing = []
    for ent in proper_nouns(fact, allowlist):
        low = ent.lower()
        if low in src:
            continue
        # #57: spelling normalisation — "Wi-Fi" grounds off "wifi" and the
        # reverse, comparing hyphen-stripped forms of both sides.
        if _dehyphen(low) in src_dehyphened:
            continue
        pairs = zip(re.split(r"[\s\-/.]+", low), re.split(r"[\s\-/.]+", ent))
        if any(lt and _token_supported(lt, ot, src) for lt, ot in pairs):
            continue
        missing.append(ent)
    return missing


# ── temporal grounding (#38) ─────────────────────────────────────────────────
#
# Explicit calendar dates we recognise IN TEXT. One definition, two consumers:
# mining grounds a model-supplied `event=` against it (a date is honoured only
# when literally present — #30/#32/#33), and the temporal wall below uses it to
# catch a date asserted in the fact's PROSE that the source never wrote.
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}
_MONTH_ALT = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
              r"jul(?:y)?|aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|"
              r"nov(?:ember)?|dec(?:ember)?")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# "June 30", "June 30, 2026", "Jul 4th"
_MDY_RE = re.compile(
    rf"\b({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s+(\d{{4}}))?\b",
    re.I)
# "30 June", "13th of July", "1 Jan 2027"
_DMY_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)\s+(?:of\s+)?({_MONTH_ALT})"
    rf"(?:,?\s+(\d{{4}}))?\b",
    re.I)

# Relative time phrases. These are the ones a fact must not INVENT: they only
# mean anything against a moment, so a gloss the source never wrote ("(tomorrow
# from the conversation date)") is an assertion nobody made. Capitalised
# weekdays are already covered by the proper-noun wall above, so this list is
# deliberately the lower-case remainder plus explicit intervals.
_REL_TIME_RE = re.compile(
    r"\b(tomorrow|yesterday|tonight|"
    r"(?:next|last|this)\s+(?:week|month|year|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday)|"
    r"in\s+\d+\s+(?:day|week|month|year)s?|"
    r"\d+\s+(?:day|week|month|year)s?\s+(?:ago|away|from\s+now|later))\b", re.I)


def explicit_dates(text: str) -> set:
    """Every explicit calendar date literally present in `text`, as
    (month, day, year_or_None) tuples. Year is None when the phrase names no
    year ("June 30") — the caller resolves it from conversation context, never
    the model. Malformed values (e.g. 2026-13-40) are discarded."""
    found = set()

    def add(month: int, day: int, year):
        try:  # reject impossible day/month combinations
            datetime(year or 2000, month, day)
        except ValueError:
            return
        found.add((month, day, year))

    for y, mo, d in _ISO_DATE_RE.findall(text):
        add(int(mo), int(d), int(y))
    for mon, d, y in _MDY_RE.findall(text):
        add(_MONTHS[mon.lower()], int(d), int(y) if y else None)
    for d, mon, y in _DMY_RE.findall(text):
        add(_MONTHS[mon.lower()], int(d), int(y) if y else None)
    return found


def ungrounded_temporal(fact: str, source_text: str) -> list[str]:
    """Temporal claims in `fact` that `source_text` never makes (#38).

    The grounding wall above checks proper nouns, so it never saw a date: a
    lower-case gloss like "the appointment is Saturday (tomorrow from the
    conversation date)" passed every wall while asserting a schedule nobody
    stated. The weekday survived and the interval was invented — broadly
    correct biography, wrong near-term planning, at high confidence.

    Two classes, both "did the source actually say this":
      · an explicit calendar date in the fact that appears in no source date;
      · a relative phrase (tomorrow / in 9 days / next Saturday) absent from
        the source.

    Comparing dates SEMANTICALLY, not as strings — "June 30" in the source
    grounds "2026-06-30" in the fact, because they are the same day written
    differently. A fact that keeps the source's own qualifier verbatim always
    passes, which is the behaviour we want: the reader gets the original phrase
    plus the fact's date and can resolve it themselves, instead of trusting an
    absolute date this service inferred.
    """
    missing = []
    src_dates = {(mo, dy) for mo, dy, _ in explicit_dates(source_text or "")}
    for mo, dy, _ in explicit_dates(fact or ""):
        if (mo, dy) not in src_dates:
            missing.append(f"{mo:02d}-{dy:02d}")
    src_rel = {m.group(0).lower() for m in _REL_TIME_RE.finditer(source_text or "")}
    for m in _REL_TIME_RE.finditer(fact or ""):
        phrase = m.group(0).lower()
        if phrase not in src_rel:
            missing.append(m.group(0))
    return missing


def source_trusted(source_text: str) -> bool:
    """False if the chat is roleplay/persona/prep or an AI dossier — its 'facts'
    may be fiction or the model's own claims, not biography."""
    return not _PERSONA_RE.search(source_text or "")


# ── speaker classes (#31) ────────────────────────────────────────────────────
#
# Ingest speaker values, classified once, here, for every consumer. The wire
# grammar is additive to contract 1.1: `user` and bare model slugs are the
# classes that always existed; `guest:<name>` and `guest:unknown` arrived with
# multi-human voice sessions (crossband room mode). Anything else that carries
# a class prefix is a class this build does not know, and fail-safe means it
# behaves as untrusted rather than silently gaining the owner's trust.

def speaker_class(speaker: str) -> str:
    """Classify one ingest `speaker` value.

    'owner'         - "user": the human this ledger is about.
    'model'         - a bare participant slug ("claude"): an AI seat.
    'guest'         - "guest:<name>": another, named human in the session.
    'guest-unknown' - "guest:unknown" (or a nameless "guest:"): a human turn
                      diarization could not attribute confidently.
    'unrecognised'  - any other class-prefixed or empty value: a speaker class
                      this build does not know. Treated as untrusted.
    """
    s = (speaker or "").strip()
    if s == "user":
        return "owner"
    if ":" not in s:
        return "model" if s else "unrecognised"
    prefix, name = s.split(":", 1)
    if prefix.strip().lower() == "guest":
        name = name.strip()
        if not name or name.lower() == "unknown":
            return "guest-unknown"
        return "guest"
    return "unrecognised"


def guest_name(speaker: str) -> str | None:
    """The guest's name from a `guest:<name>` speaker; None for every other
    class (including `guest:unknown`, which by definition has no name)."""
    if speaker_class(speaker) != "guest":
        return None
    return speaker.strip().split(":", 1)[1].strip()


def speaker_trust_flag(speaker: str) -> str | None:
    """The source-trust wall applied to WHO SPOKE (#31). None for the two
    classes mining has always consumed (the owner's `user` turns and the model
    seats); otherwise a quarantine flag naming the speaker, so a fact drawn
    from that turn is written but held for review — the same posture `mcp:*`
    writes get from the ledger gate. `guest:unknown` and unrecognised classes
    quarantine unconditionally: no name means no one to ever earn trust."""
    cls = speaker_class(speaker)
    if cls in ("owner", "model"):
        return None
    if cls == "guest":
        return (f"guest-attribution: stated by guest {guest_name(speaker)}, "
                "not the owner")
    if cls == "guest-unknown":
        return ("guest-attribution: speaker unidentified (diarization could "
                "not attribute this turn confidently)")
    return (f"speaker-trust: unrecognised speaker class {speaker!r}, "
            "treated as untrusted")


# ── lexical support (used to verify a retry-supplied src= binding) ───────────
#
# Function words carry no attribution signal: they appear in nearly every turn,
# so counting them as "support" would let an unrelated turn look like a fact's
# source. Only words of four or more characters are considered at all, which
# already removes most of English's connective tissue; this list is the common
# remainder plus the vague verbs a paraphrase reaches for.
_SUPPORT_STOPWORDS = {
    "about", "actually", "after", "again", "also", "another", "anything",
    "back", "basically", "because", "been", "before", "being", "both", "came",
    "come", "could", "days", "does", "doing", "done", "down", "during", "each",
    "else", "even", "evening", "ever", "every", "everything", "from", "gets",
    "give", "goes", "going", "gone", "have", "having", "here", "hour", "hours",
    "into", "just", "keep", "kind", "know", "later", "like", "look", "made",
    "make", "many", "maybe", "might", "month", "months", "more", "morning",
    "most", "much", "must", "need", "next", "night", "okay", "over",
    "probably", "really", "said", "same", "says", "should", "since", "some",
    "someone", "something", "still", "stuff", "such", "sure", "take", "than",
    "that", "them", "then", "there", "these", "they", "thing", "things",
    "think", "this", "those", "through", "time", "today", "tonight",
    "tomorrow", "under", "very", "want", "week", "weeks", "well", "went",
    "were", "what", "when", "where", "which", "while", "will", "with",
    "would", "year", "years", "yeah", "yesterday", "your",
}
_WORD_RE = re.compile(r"[a-z0-9]+")


def support_tokens(text: str, allowlist: set[str]) -> set[str]:
    """The distinctive content words of `text`: lower-case, four characters or
    more, minus function words and anything on the caller's allowlist (which
    carries the owner's own name and their everyday nouns — words that recur in
    every fact and would flatten the comparison below). Allowlist entries are
    split into words too, so a two-word name excludes both of its parts."""
    allowed = {w for entry in allowlist for w in _WORD_RE.findall(entry.lower())}
    return {t for t in _WORD_RE.findall((text or "").lower())
            if len(t) >= 4 and t not in _SUPPORT_STOPWORDS and t not in allowed}


def lexical_support(fact: str, source_text: str, allowlist: set[str]) -> int:
    """How many of `fact`'s distinctive content words `source_text` actually
    contains — a coarse "did this wording come from here" signal.

    Deliberately NOT a truth test and never used as one. Its single job is to
    sanity-check a src= binding the miner supplied on a corrective retry (#35),
    where the alternative is trusting an unverifiable guess about WHO SPOKE.
    Exact word matches only: no stemming, no synonyms, no fuzzy matching, so a
    heavy paraphrase scores low. That bias is the right way round, because the
    only thing a low score can do in `mining` is HOLD a fact for review — it can
    never drop one, and it can never grant trust a wall would otherwise refuse.
    """
    f = support_tokens(fact, allowlist)
    if not f:
        return 0
    return len(f & support_tokens(source_text, allowlist))


def is_system_meta(fact: str) -> bool:
    """True when the 'fact' is about this memory system's own machinery or the
    AI tooling itself, not the user. Such lines are DROPPED at extraction (never
    quarantined) — they are not biography and there is nothing to review (#36)."""
    return bool(_SYSTEM_META_RE.search(fact or ""))


def is_builder_process(fact: str) -> bool:
    """True when the 'fact' is ordinary engineering/execution chatter about
    building a project — PR/issue mechanics, code review/CI status,
    implementation minutiae, or transient UI styling/layout detail (#66). Such
    lines are DROPPED at extraction (never quarantined), same as
    `is_system_meta`: they aren't biography about the user, so there is
    nothing for a human to approve. Deliberately narrow — it does not match a
    durable preference, career decision, or meaningful project outcome, which
    stays eligible and is judged by the quarantine walls like anything else."""
    return bool(_BUILDER_PROCESS_RE.search(fact or ""))


def in_scope(fact: str) -> bool:
    """False if the fact is about this system's own state, not the user.
    (Back-compat wrapper around the negation of is_system_meta.)"""
    return not is_system_meta(fact)


def check(fact: str, source_text: str, allowlist: set[str]) -> list[str]:
    """The three QUARANTINE walls (grounding, temporal grounding, source-trust).
    Returns flag descriptions; empty list = clean. System/meta scope is handled
    separately by is_system_meta, which DROPS rather than quarantines (#36)."""
    flags = []
    missing = ungrounded_entities(fact, source_text, allowlist)
    if missing:
        flags.append("grounding: " + ", ".join(missing) + " not in source chat")
    when = ungrounded_temporal(fact, source_text)
    if when:
        flags.append("temporal: " + ", ".join(when) + " not stated in source chat")
    if not source_trusted(source_text):
        flags.append("source-trust: chat contains roleplay/persona/dossier framing")
    return flags
