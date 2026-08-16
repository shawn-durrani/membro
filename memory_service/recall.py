"""Hybrid recall — semantic + keyword + bounded recency, duplicates collapsed.

Quarantined facts are always excluded; superseded excluded unless asked for
(audit). Recency ranks current facts above stale-but-similar history without
hiding the old facts — ranking, not resolution.
"""

from . import db, embeddings

SEMANTIC_FLOOR = 0.25
PARAPHRASE_CUTOFF = 0.93
RECENCY_WEIGHT = 0.10  # bounded: never buries a relevant old fact


def _recency(entry: dict, now_ts: float) -> float:
    ts = entry.get("event_date") or entry.get("created_at") or 0
    if not ts:
        return 0.0
    age_days = max(0.0, (now_ts - ts) / 86400.0)
    return max(0.0, 1.0 - age_days / 1095.0)  # linear to zero at ~3 years


def recall(con, settings, query: str = "", limit: int = 10,
           include_superseded: bool = False) -> list[dict]:
    query = (query or "").strip()
    if not query:
        rows = con.execute(
            "SELECT * FROM facts WHERE quarantined_at IS NULL"
            + ("" if include_superseded else " AND invalidated_at IS NULL")
            + " ORDER BY id DESC LIMIT ?", (limit,))
        return [_out(dict(r), None) for r in rows]

    try:
        embeddings.ensure_fact_embeddings(con, settings)
    except Exception:
        pass  # semantic degrades; keyword still works
    entries = [dict(r) for r in con.execute(
        "SELECT * FROM facts WHERE quarantined_at IS NULL")]
    if not entries:
        return []

    qvec = None
    try:
        qvec = embeddings.embed_query(query, settings)
    except Exception:
        qvec = None
    # normalize the query ONCE — the scan compares it against every fact,
    # and recomputing its norm per fact was ~a third of the scan (#78)
    qunit = embeddings.unit(qvec) if qvec is not None else None
    words = [w.lower() for w in query.split() if len(w) >= 3]
    now_ts = db.now()
    scored = []
    for e in entries:
        if e.get("invalidated_at") and not include_superseded:
            continue
        sem, ev = 0.0, None
        if qunit is not None and e.get("embedding"):
            ev = embeddings.unpack(e["embedding"])
            if len(ev) != len(qunit):
                ev = None  # stale space (#60): treat the vector as missing
            else:
                sem = embeddings.cosine_unit(qunit, ev)
        kw = sum(1 for w in words if w in e["content"].lower())
        score = (sem + 0.12 * min(kw, 3)
                 + (0.05 if not e.get("invalidated_at") else 0)
                 + RECENCY_WEIGHT * _recency(e, now_ts))
        if sem > SEMANTIC_FLOOR or kw:
            e["_vec"] = ev  # reused by the dedup below; stripped in _out
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])

    # collapse exact (hash) + paraphrase (cosine) duplicates, keep highest-scored
    kept, kept_vecs, seen_hash = [], [], set()
    for score, e in scored:
        h = e.get("content_hash")
        if h and h in seen_hash:
            continue
        # unpacked once in the scoring pass (#78); the fallback covers the
        # keyword-only path (no query vector), where facts may still carry
        # embeddings and paraphrase collapse must keep working
        ev = e.get("_vec")
        if ev is None and e.get("embedding"):
            ev = embeddings.unpack(e["embedding"])
        if ev is not None and any(embeddings.cosine(ev, kv) > PARAPHRASE_CUTOFF
                                  for kv in kept_vecs):
            continue
        kept.append(_out(e, round(score, 4)))
        if h:
            seen_hash.add(h)
        if ev is not None:
            kept_vecs.append(ev)
        if len(kept) >= limit:
            break
    return kept


def _out(e: dict, score) -> dict:
    e.pop("embedding", None)
    e.pop("_vec", None)
    if score is not None:
        e["score"] = score
    return e
