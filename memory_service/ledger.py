"""The ledger — append-only fact store with temporal validity and the write gate.

No function in this module deletes a fact. Supersede, quarantine, and dismiss
are all reversible state changes; hard delete exists only as the human-initiated
API endpoint.
"""

import json
import logging
import re
import threading

from . import db, walls

log = logging.getLogger("memory_service.ledger")

# `#1990` / `#216, 1394 2039` — an id lookup rather than a content search.
# The `#` is what disambiguates: a bare `1990` is a legitimate thing to search
# the TEXT for (a year, a figure), and silently reading it as an id would make
# content search unpredictable.
_ID_QUERY = re.compile(r"^#\s*(\d[\d,\s]*)$")


def parse_id_query(query: str) -> list[int] | None:
    """Ids from an `#id[,id...]` query, or None if this is a text search."""
    m = _ID_QUERY.match((query or "").strip())
    if not m:
        return None
    ids = [int(p) for p in re.split(r"[,\s]+", m.group(1)) if p]
    return ids or None


def _embed_now(db_path, fact_id: int, content: str, settings) -> None:
    """Embed one fact right after it's written, so the NEXT recall never pays
    the provider round-trip in its hot path (found in the 2026-07-09 latency
    review: the recall after any save stalled ~150-500ms). Own connection —
    runs on a background thread. Best-effort: on any failure the recall-path
    ensure_fact_embeddings remains the safety net."""
    from . import embeddings
    try:
        vecs = embeddings.embed_texts([content], settings)
        if not vecs:
            return
        con = db.connect(db_path)
        try:
            con.execute("UPDATE facts SET embedding=? WHERE id=? AND embedding IS NULL",
                        (embeddings.pack(vecs[0]), fact_id))
            con.commit()
        finally:
            con.close()
    except Exception:
        log.debug("write-time embed for fact %s deferred to recall path", fact_id)


def _spawn_embed(settings, fact_id: int, content: str) -> None:
    from . import embeddings
    if not embeddings.available(settings):
        return  # no provider: recall degrades to keyword-only anyway
    threading.Thread(target=_embed_now, name=f"embed-{fact_id}", daemon=True,
                     args=(settings.db_path, fact_id, content, settings)).start()


def is_trusted(origin_agent: str | None, source_app: str | None, settings) -> bool:
    """The write gate's trust rule: the human, and registered client apps.
    Everything else (any MCP client, unknown callers) is held for review.

    An `mcp:*` origin is NEVER trusted — not even when the write declares a
    registered `source_app`. Invariant #4 gates the write, not the writer: an
    untrusted adapter (or any caller spoofing that origin over the local HTTP
    API) must not be able to launder a fact straight into canon simply by
    naming a trusted app. Registered clients forward the human's own curated
    model facts under a plain participant slug, never an `mcp:*` origin, so
    this closes the bypass without touching the legitimate trusted path."""
    if origin_agent == "user":
        return True
    if origin_agent and origin_agent.startswith("mcp:"):
        return False
    return source_app in set(settings.trusted_apps)


def add_fact(con, content: str, settings, *, source: str = "user",
             origin_agent: str | None = None, source_app: str | None = None,
             event_date: float | None = None, confidence: str = "high",
             importance: int | None = None, conversation_id: int | None = None,
             source_message_id: int | None = None,
             quarantine_reason: str | None = None,
             web_sources: list[str] | None = None,
             guest_speakers: list[str] | None = None,
             dedupe_in_conversation: bool = False,
             scope: str | None = None) -> dict:
    """Append one fact. Untrusted origins are quarantined at creation (the gate);
    a caller-supplied quarantine_reason (e.g. a wall flag) also quarantines.

    `scope` (#72): `global` is recalled everywhere, `conversation` only from
    the conversation the fact came from. Left None it is worked out here: a
    fact drawn from a guest's turn, or saved while guests were in the room,
    is bound to its conversation; the owner's own facts stay global, as
    every fact was before the column existed.

    `web_sources` (#55, contract 1.3): domains the authoring round read from
    the web - passed explicitly by /v1/facts, and inherited automatically
    from the source message's stamp on the mining path. Non-empty means the
    fact is held with a `web-derived:` reason: a public page must not write
    memory by phrasing a sentence well. The origin gate outranks it.

    `guest_speakers` (#93, contract 1.5): the guests in the room when a model
    saved this directly, as `/ingest` speaker-class values (`guest:<name>`,
    `guest:unknown`). The mined path already holds a guest's words because
    each message names its speaker; a direct save named nobody, so a guest's
    claim relayed by a model reached canon unheld. Non-empty means the fact
    is held with a `guest-present:` reason. The origin gate outranks it, and
    when a web stamp is also present web-derived keeps the reason slot with
    the guest clause appended, so the review class stays web-derived.

    `dedupe_in_conversation` is a NARROW write-time guard for the mining path
: when set, re-adding a fact whose normalized content already exists
    (non-superseded) for the SAME conversation is a no-op returning the existing
    row. It stops a re-run that re-mines the same messages from multiplying facts
    — the case the in-process distill lock cannot cover (a crash/restart between
    adding facts and advancing the watermark). It is deliberately scoped to a
    single conversation and OFF by default: human saves and cross-conversation
    re-mention (a real freshness signal) stay insertable, and exact-duplicate
    hygiene across the whole ledger remains consolidate.py's advisory, reversible
    job — this guard never supplants it."""
    content = " ".join((content or "").split())
    if len(content) < 8:
        raise ValueError("nothing meaningful to save")
    if len(content) > 10_000:
        raise ValueError("fact too long (max 10000 chars) — a memory is one sentence")
    origin = origin_agent or "user"
    reason = quarantine_reason
    if reason is None and not is_trusted(origin, source_app, settings):
        reason = (f"{ORIGIN_HOLD_PREFIX}{origin}) — held for review before "
                  "becoming canon")
        confidence = "low"
    if dedupe_in_conversation and conversation_id is not None:
        dup = con.execute(
            "SELECT id, quarantined_at FROM facts WHERE conversation_id=? "
            "AND content_hash=? AND invalidated_at IS NULL "
            "ORDER BY id LIMIT 1",
            (conversation_id, db.content_hash(content))).fetchone()
        if dup is not None:
            # Already remembered from this same conversation (a prior distill of
            # the same messages) — return the existing row, insert nothing.
            return {"id": dup["id"], "quarantined": bool(dup["quarantined_at"]),
                    "duplicate": True}
    ts = db.now()
    prov = source_provenance(con, source_message_id)
    person_id = prov["person_id"]
    webs = [str(w).strip().lower() for w in (web_sources or []) if str(w).strip()]
    webs += prov["web_sources"]
    if reason is None and webs:
        shown = ", ".join(sorted(set(webs))[:5])[:300]
        reason = (f"web-derived: {shown} — a public page was read in this "
                  "round; held for review before becoming canon")
    guests = guest_list(guest_speakers)
    if guests:
        who = _render_guests(guests)
        verb = "was" if len(guests) == 1 else "were"
        clause = (f"guest-present: {who} {verb} in the room when a model "
                  "saved this; held for review before it becomes canon")
        if reason is None:
            reason = clause
        elif reason.startswith("web-derived:"):
            # Both stamps on one save: web-derived claimed the slot first,
            # and reason_class() reads the first flag, so the queue keeps
            # grouping it under the web page. The guest clause still rides
            # the row for the reviewer.
            reason = f"{reason}; {clause}"
    if scope is None:
        from . import walls
        speaker_cls = (walls.speaker_class(prov["speaker"]) if prov["found"]
                       else None)
        from_guest = bool(guests) or speaker_cls in ("guest", "guest-unknown")
        scope = ("conversation" if from_guest and conversation_id is not None
                 else "global")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    cur = con.execute(
        "INSERT INTO facts(content, source, origin_agent, conversation_id, "
        "source_message_id, created_at, event_date, confidence, importance, "
        "content_hash, quarantined_at, quarantine_reason, person_id, scope) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (content, source, origin, conversation_id, source_message_id, ts,
         event_date if event_date is not None else db.day_start(ts),
         confidence, importance, db.content_hash(content),
         ts if reason else None, reason, person_id, scope))
    con.commit()
    _spawn_embed(settings, cur.lastrowid, content)
    return {"id": cur.lastrowid, "quarantined": bool(reason), "scope": scope}


def source_provenance(con, source_message_id: int | None) -> dict:
    """What a fact takes from the message it's bound to: the turn's speaker,
    the web domains stamped on it, and the person its speaker identity links
    to. `add_fact` reads this for a fact born bound, and `bind_source` for
    one bound later, so the two can't drift apart.

    #55: the mining path inherits the web stamp from the turn itself, so the
    miner needs no knowledge of that rule. #33 contract 1.2: a structured
    speaker identity links the fact to that person when the identity is
    strong enough (persons.binding - human-confirmed always, voice-match at
    0.8+, weaker never). The link changes what review can say, never
    whether a fact is held."""
    out = {"found": False, "speaker": None, "web_sources": [],
           "person_id": None}
    if source_message_id is None:
        return out
    row = con.execute("SELECT speaker, speaker_identity, web_sources "
                      "FROM messages WHERE id=?",
                      (source_message_id,)).fetchone()
    if row is None:
        return out
    out["found"] = True
    out["speaker"] = row["speaker"]
    if row["web_sources"]:
        try:
            out["web_sources"] = [str(w).strip().lower()
                                  for w in json.loads(row["web_sources"])
                                  if str(w).strip()]
        except ValueError:
            pass
    if row["speaker_identity"]:
        from . import persons as persons_mod
        try:
            out["person_id"] = persons_mod.binding(
                con, json.loads(row["speaker_identity"]))
        except (ValueError, KeyError):
            out["person_id"] = None
    return out


def bind_refusal(con, message_id: int) -> str | None:
    """Why `bind_source` won't bind a fact to this message, or None.

    A fact born bound takes more from its turn than the link. A web stamp
    holds it for review, a speaker identity links it to a person, and a
    guest's or unrecognised speaker's turn makes the miner hold it and
    scopes it to its chat. A late binding may change the link and the date
    and nothing else, so a turn that would bring any of those is refused."""
    prov = source_provenance(con, message_id)
    if not prov["found"]:
        return "no such message"
    if walls.speaker_trust_flag(prov["speaker"]):
        return "the turn is a guest's or an unrecognised speaker's"
    if prov["web_sources"]:
        return "the turn carries a web stamp, which would hold the fact"
    if prov["person_id"] is not None:
        return "the turn's speaker identity would link the fact to a person"
    return None


def bind_source(con, fact_id: int, message_id: int,
                event_date: float | None = None) -> bool:
    """Record the message an unbound fact came from, and the calendar day
    that message grounds for it when the caller found one (#146).

    Only the link and the date change. Raises ValueError when the message
    would bring anything else (see `bind_refusal`). Returns False unless
    the fact is valid, has no source yet, and comes from the message's own
    conversation. The caller commits, so a batch lands in one transaction."""
    refusal = bind_refusal(con, message_id)
    if refusal:
        raise ValueError(refusal)
    sets, params = ["source_message_id=?"], [message_id]
    if event_date is not None:
        sets.append("event_date=?")
        params.append(event_date)
    cur = con.execute(
        f"UPDATE facts SET {', '.join(sets)} WHERE id=? "
        "AND source_message_id IS NULL AND invalidated_at IS NULL "
        "AND quarantined_at IS NULL AND conversation_id="
        "(SELECT conversation_id FROM messages WHERE id=?)",
        (*params, fact_id, message_id))
    return cur.rowcount > 0


SCOPES = ("global", "conversation")


def set_scope(con, fact_id: int, scope: str) -> bool:
    """Rebind a fact (#72): `global` lets it be recalled everywhere, which
    is how the owner adopts a guest's fact as their own; `conversation`
    binds it back. Human-only (API/UI); never changes the hold state."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    cur = con.execute("UPDATE facts SET scope=? WHERE id=?", (scope, fact_id))
    con.commit()
    return cur.rowcount > 0


def guest_list(guest_speakers) -> list[str]:
    """Normalise a `guest_speakers` stamp the way web_sources is: strip, drop
    empties, dedupe, keep order. Anything that is not a guest class (a model
    slug, `user`, an unknown prefix) is dropped rather than rejected: the
    field is a presence stamp, and a client that sends the wrong shape has
    not made a claim this ledger can hold on."""
    out: list[str] = []
    for g in guest_speakers or []:
        g = str(g).strip()
        if walls.speaker_class(g) not in ("guest", "guest-unknown"):
            continue
        if g not in out:
            out.append(g)
    return out


def _render_guests(guests: list[str]) -> str:
    """Plain English for the hold reason: names from `guest:<name>`, and
    `guest:unknown` as an unidentified guest. Capped like the web-derived
    reason (five entries, 300 characters) so one save cannot pad a row."""
    names = [walls.guest_name(g) or "an unidentified guest" for g in guests[:5]]
    if len(names) == 1:
        return names[0][:300]
    return (", ".join(names[:-1]) + " and " + names[-1])[:300]


def list_facts(con, status: str = "valid", query: str | None = None,
               limit: int = 100, before: int | None = None) -> list[dict]:
    """Facts newest first. `before` is a fact id, and only older ids come
    back, so a page after the last one shown is `before=<its id>` (#190).
    It's a cursor rather than an offset: a fact saved between two pages
    doesn't shift the next one, so no row shows twice."""
    where, params = [], []
    if status == "valid":
        where.append("invalidated_at IS NULL AND quarantined_at IS NULL")
    elif status == "superseded":
        where.append("invalidated_at IS NOT NULL")
    elif status == "quarantined":
        where.append("quarantined_at IS NOT NULL")
    ids = parse_id_query(query) if query else None
    if ids:
        where.append(f"id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
        # An explicit id list is a request for exactly those rows, so it must
        # never be silently trimmed by the page size — the caller would think
        # the missing ids simply don't exist.
        limit = max(limit, len(ids))
    elif query:
        where.append("content LIKE ?")
        params.append(f"%{query}%")
    if before is not None and not ids:
        where.append("id < ?")
        params.append(before)
    sql = "SELECT * FROM facts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [_public(r) for r in con.execute(sql, params)]


def scope_filter(conversation_id: int | None) -> tuple[str, tuple]:
    """The SQL clause (and its args) for the facts one conversation may see
    (#72): the global ones, plus those bound to `conversation_id`, membro's
    internal id. With no conversation, global only. Recall and the miner's
    list both read through this, so a bound fact stays in its own chat."""
    if conversation_id is not None:
        return " AND (scope='global' OR conversation_id=?)", (conversation_id,)
    return " AND scope='global'", ()


def valid_facts_from(con, floor_id: int,
                     conversation_id: int | None = None) -> list[dict]:
    """Every valid fact with an id at or above `floor_id` that
    `conversation_id` may see, oldest first."""
    scope_sql, scope_args = scope_filter(conversation_id)
    return [_public(r) for r in con.execute(
        "SELECT * FROM facts WHERE invalidated_at IS NULL "
        "AND quarantined_at IS NULL AND id >= ?" + scope_sql + " ORDER BY id",
        (floor_id, *scope_args))]


def get_fact(con, fact_id: int) -> dict | None:
    row = con.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
    return _public(row) if row else None


def update_fact(con, fact_id: int, *, content: str | None = None,
                event_date: float | None = None,
                confidence: str | None = None,
                importance: int | None = None, settings=None) -> bool:
    sets, params = [], []
    if content is not None:
        content = " ".join(content.split())
        sets += ["content=?", "content_hash=?", "embedding=NULL"]
        params += [content, db.content_hash(content)]
    if event_date is not None:
        sets.append("event_date=?")
        params.append(event_date)
    if confidence is not None:
        sets.append("confidence=?")
        params.append(confidence)
    if importance is not None:
        # The human outranks the miner: importance decides how a fact ages
        # (a slower decay + durable-pool membership), so its owner must be
        # able to correct it. Clamped to the 1-10 scale.
        sets.append("importance=?")
        params.append(min(10, max(1, int(importance))))
    if not sets:
        return False
    params.append(fact_id)
    cur = con.execute(f"UPDATE facts SET {', '.join(sets)} WHERE id=?", params)
    con.commit()
    if cur.rowcount and content is not None and settings is not None:
        _spawn_embed(settings, fact_id, content)  # re-embed off the hot path
    return cur.rowcount > 0


def mark_superseded(con, old_id: int, new_id: int) -> bool:
    cur = con.execute(
        "UPDATE facts SET invalidated_at=?, superseded_by=? "
        "WHERE id=? AND invalidated_at IS NULL", (db.now(), new_id, old_id))
    con.commit()
    return cur.rowcount > 0


def quarantine(con, fact_id: int, reason: str) -> bool:
    cur = con.execute(
        "UPDATE facts SET quarantined_at=?, quarantine_reason=? "
        "WHERE id=? AND quarantined_at IS NULL", (db.now(), reason, fact_id))
    con.commit()
    return cur.rowcount > 0


def quarantine_many(con, fact_ids, reason: str) -> dict:
    """Bulk `quarantine`, for facts ALREADY accepted as canon.

    `quarantine` above is reached only at ingest, by the wall. Nothing could
    quarantine a fact that had already been accepted — leaving `DELETE`
    (destructive) or `supersede` (which asserts a replacement fact that, for a
    malformed row, does not exist) as the only treatments. This is the missing
    third option: pull it out of recall + summary, keep it in the ledger, and
    put it in the review queue with a reason.

    Batch because the motivating cases are batch: malformed facts left
    behind by a mining regression, and the stale-canon cleanups that follow.
    Unknown or already-quarantined ids are skipped rather than failing the
    call, so a re-run is a no-op. Returns which ids were acted on and which
    were skipped. Non-destructive, reversible via `approve`. Human-only."""
    wanted = list(dict.fromkeys(int(i) for i in fact_ids))
    if not wanted:
        return {"quarantined": [], "skipped": []}
    marks = ",".join("?" * len(wanted))
    eligible = {r["id"] for r in con.execute(
        f"SELECT id FROM facts WHERE id IN ({marks}) AND quarantined_at IS NULL",
        wanted)}
    if eligible:
        ids = sorted(eligible)
        con.execute(
            f"UPDATE facts SET quarantined_at=?, quarantine_reason=? "
            f"WHERE id IN ({','.join('?' * len(ids))})",
            [db.now(), reason, *ids])
        con.commit()
    return {"quarantined": [i for i in wanted if i in eligible],
            "skipped": [i for i in wanted if i not in eligible]}


# The two holds that mark a fact low confidence as they hold it (#195): the
# origin gate in add_fact, and the miner's checks, whose reasons all end with
# MINER_HOLD_SUFFIX. The em-dash is stored data, not prose. Every other hold
# (web or guest stamps, the owner's own, a forget, an erased source) keeps
# the confidence the fact was saved with.
ORIGIN_HOLD_PREFIX = "external write ("
MINER_HOLD_SUFFIX = " — review before trusting"
# What a fact gets when nothing holds it: a clean mined fact, and a direct
# save's default on POST /v1/facts and over MCP.
UNHELD_CONFIDENCE = "high"


def hold_lowered_confidence(reason: str | None) -> bool:
    """Whether the hold behind this reason is one that set the fact's
    confidence to low."""
    reason = reason or ""
    return (reason.startswith(ORIGIN_HOLD_PREFIX)
            or reason.endswith(MINER_HOLD_SUFFIX))


def approve(con, fact_id: int) -> bool:
    """Un-quarantine: the fact rejoins recall + summary. Human-only (API/UI).

    A hold that marked the fact low confidence is undone with it (#195): the
    fact comes back at UNHELD_CONFIDENCE, so an approved fact doesn't read
    as doubtful to every AI that recalls it. A confidence the owner changed
    while the fact was held is left as they set it."""
    row = con.execute("SELECT quarantine_reason, confidence FROM facts "
                      "WHERE id=?", (fact_id,)).fetchone()
    if row is None:
        return False
    confidence = row["confidence"]
    if confidence == "low" and hold_lowered_confidence(row["quarantine_reason"]):
        confidence = UNHELD_CONFIDENCE
    cur = con.execute(
        "UPDATE facts SET quarantined_at=NULL, quarantine_reason=NULL, "
        "review_dismissed_at=NULL, confidence=? WHERE id=?",
        (confidence, fact_id))
    con.commit()
    return cur.rowcount > 0


def dismiss(con, fact_id: int) -> bool:
    """Reviewed-and-kept-out: stays quarantined and in the ledger, leaves the
    review queue. Non-destructive. Human-only (API/UI)."""
    cur = con.execute(
        "UPDATE facts SET review_dismissed_at=? WHERE id=? AND quarantined_at IS NOT NULL",
        (db.now(), fact_id))
    con.commit()
    return cur.rowcount > 0


def dismiss_all(con) -> int:
    """Bulk `dismiss`: every fact currently sitting in the review queue,
    reviewed-and-kept-out in one action. Exact same semantics as one-at-a-time
    dismiss (quarantined, non-destructive, reversible via `approve`) — just
    applied to the whole queue at once, for clearing a backlog made stale by a
    filtering fix without clicking through each row. Human-only (API/UI).
    Returns the number of facts moved out of the queue."""
    cur = con.execute(
        "UPDATE facts SET review_dismissed_at=? "
        "WHERE quarantined_at IS NOT NULL AND review_dismissed_at IS NULL",
        (db.now(),))
    con.commit()
    return cur.rowcount


def review_queue(con) -> list[dict]:
    return [_public(r) for r in con.execute(
        "SELECT * FROM facts WHERE quarantined_at IS NOT NULL "
        "AND review_dismissed_at IS NULL ORDER BY id DESC")]


def _public(row) -> dict:
    d = dict(row)
    d.pop("embedding", None)  # internal representation, never serialized out
    return d
