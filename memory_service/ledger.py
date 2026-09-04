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
             dedupe_in_conversation: bool = False) -> dict:
    """Append one fact. Untrusted origins are quarantined at creation (the gate);
    a caller-supplied quarantine_reason (e.g. a wall flag) also quarantines.

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
        reason = f"external write ({origin}) — held for review before becoming canon"
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
    # #33 contract 1.2: a fact born from a message that carries a
    # structured speaker identity links to that person when the identity
    # is strong enough (persons.binding - human-confirmed always,
    # voice-match at 0.8+, weaker never). Same hold rules either way:
    # the link changes what review can say, never whether a fact is held.
    person_id = None
    webs = [str(w).strip().lower() for w in (web_sources or []) if str(w).strip()]
    if source_message_id is not None:
        row = con.execute("SELECT speaker_identity, web_sources FROM messages "
                          "WHERE id=?", (source_message_id,)).fetchone()
        if row and row["web_sources"]:
            # #55: the mining path inherits the stamp from the turn itself,
            # so the miner needs no knowledge of this rule.
            try:
                webs += [str(w).strip().lower()
                         for w in json.loads(row["web_sources"]) if str(w).strip()]
            except ValueError:
                pass
        if row and row["speaker_identity"]:
            from . import persons as persons_mod
            try:
                person_id = persons_mod.binding(
                    con, json.loads(row["speaker_identity"]))
            except (ValueError, KeyError):
                person_id = None
    if reason is None and webs:
        shown = ", ".join(sorted(set(webs))[:5])[:300]
        reason = (f"web-derived: {shown} — a public page was read in this "
                  "round; held for review before becoming canon")
    guests = _guest_list(guest_speakers)
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
    cur = con.execute(
        "INSERT INTO facts(content, source, origin_agent, conversation_id, "
        "source_message_id, created_at, event_date, confidence, importance, "
        "content_hash, quarantined_at, quarantine_reason, person_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (content, source, origin, conversation_id, source_message_id, ts,
         event_date if event_date is not None else db.day_start(ts),
         confidence, importance, db.content_hash(content),
         ts if reason else None, reason, person_id))
    con.commit()
    _spawn_embed(settings, cur.lastrowid, content)
    return {"id": cur.lastrowid, "quarantined": bool(reason)}


def _guest_list(guest_speakers) -> list[str]:
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
               limit: int = 100) -> list[dict]:
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
    sql = "SELECT * FROM facts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [_public(r) for r in con.execute(sql, params)]


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
        # (half-life stretch + durable-pool membership), so its owner must be
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


def approve(con, fact_id: int) -> bool:
    """Un-quarantine: the fact rejoins recall + summary. Human-only (API/UI)."""
    cur = con.execute(
        "UPDATE facts SET quarantined_at=NULL, quarantine_reason=NULL, "
        "review_dismissed_at=NULL WHERE id=?", (fact_id,))
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
