"""Advisory consolidation sweep — proposes, never silently acts.

Scope: exact-duplicate detection (content_hash groups) and pin nominations
(permanent-class facts the utility model spots among the high-importance
cards — the system finds, only the owner grants importance 10). Embedding-
cluster supersession/synthesis proposals come with the weighting build.
Sensitive categories are always human-review; nothing here mutates the ledger.
"""

import logging
import re

from . import llm
from .weighting import DEFAULT_IMPORTANCE, PINNED

log = logging.getLogger("memory_service.consolidate")


def _pin_nominations(con, settings) -> list[dict]:
    """Ask the utility model which high-importance facts are PERMANENT-class
    (true until explicitly changed). Conservative by instruction; degrades to
    no nominations without a key. Proposals only — pinning stays a human act."""
    rows = [dict(r) for r in con.execute(
        "SELECT id, content, importance, event_date, created_at FROM facts "
        "WHERE invalidated_at IS NULL AND quarantined_at IS NULL "
        f"AND importance >= 7 AND importance < {PINNED} ORDER BY id")]
    if not rows:
        return []
    listing = "\n".join(f"- [{f['id']}] {f['content']}" for f in rows)
    prompt = (
        "Below are high-importance entries from a personal memory ledger. "
        "Identify the ones that are PERMANENT-class: true until explicitly "
        "changed, essentially never expiring on their own — names of family "
        "members, birthdates, immutable relationships and identity facts. "
        "NOT permanent: jobs, projects, goals, preferences, addresses, "
        "anything that drifts with life. Be conservative: when unsure, leave "
        "it out. Reply with ONLY the ids, comma-separated (e.g. 12, 47), or "
        "NONE.\n\n" + listing)
    try:
        out = llm.utility_complete(prompt, settings, max_tokens=200)
    except Exception:  # keyless / provider down: the sweep still works
        log.info("pin nominations skipped — no utility model available")
        return []
    ids = {int(m) for m in re.findall(r"\d+", out or "")}
    by_id = {f["id"]: f for f in rows}
    nominated = [by_id[i] for i in ids if i in by_id]
    return [{
        "kind": "pin-nomination",
        "fact_ids": [f["id"]],
        "keep_id": f["id"],
        "facts": [f],
        "suggestion": "looks permanent — pin it (importance 10) so it never fades",
    } for f in nominated]


def run(con, settings) -> dict:
    rows = con.execute(
        "SELECT content_hash, COUNT(*) n, GROUP_CONCAT(id) ids FROM facts "
        "WHERE invalidated_at IS NULL AND quarantined_at IS NULL "
        "GROUP BY content_hash HAVING n > 1").fetchall()
    proposals = []
    for r in rows:
        ids = [int(i) for i in r["ids"].split(",")]
        # Carry the facts themselves + an explicit keep_id: a proposal the
        # human must cross-reference against the ledger by hand isn't a
        # proposal, it's homework. The UI shows the cards and offers the
        # one-click apply; nothing here mutates anything.
        #
        # Keep the STRONGEST copy: highest importance first (importance
        # stretches a card's half-life — retiring a 7 to keep a re-minted 5
        # would make the fact decay faster), newest as the tie-breaker
        # (re-mention is a freshness signal; copies usually score the same).
        facts = [dict(f) for f in con.execute(
            "SELECT id, content, importance, event_date, created_at FROM facts "
            f"WHERE id IN ({','.join('?' * len(ids))}) "
            f"ORDER BY COALESCE(importance, {DEFAULT_IMPORTANCE}) DESC, "
            "created_at DESC, id DESC", ids)]
        proposals.append({
            "kind": "exact-duplicate",
            "fact_ids": ids,
            "keep_id": facts[0]["id"],
            "facts": facts,
            "suggestion": "keep the strongest copy, supersede the rest",
        })
    proposals += _pin_nominations(con, settings)
    return {"proposals": proposals, "clusters_scanned": len(proposals)}
