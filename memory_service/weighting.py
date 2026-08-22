"""Summary selection — recency × importance, two pools, paraphrase collapse.

The light implementation of the researched scheme (docs/REFERENCES.md):
- decay runs on event_date, the date a fact is ABOUT — so bulk-imported or
  backlog-mined old facts sort as old, whatever their insertion order;
- half-life stretches with importance (FadeMem: important facts decay slower);
- a durable pool (age-blind, by importance) is kept structurally separate from
  the active pool (by composite score) so stable history and active threads
  can't starve each other (MemGPT's working-context separation);
- near-duplicates collapse before selection (same cosine cutoff recall uses at
  read time) so frequency can't masquerade as salience.
Selection only: every fact stays in the ledger; nothing here writes.
"""

import math

from . import db, embeddings
from .recall import PARAPHRASE_CUTOFF

HALF_LIFE_BASE_DAYS = 30.0
DEFAULT_IMPORTANCE = 5     # unscored facts (e.g. imported ledgers) are neutral
PINNED = 10                # importance 10 = PERMANENT: never decays, always in
                           # the profile. Owner-only — the miner is capped at 9,
                           # so only a human can mark a fact never-forgotten.
DURABLE_N = 200            # age-blind pool: identity-level, high-importance
ACTIVE_N = 300             # composite-scored pool: recent & active threads
POOL_MARGIN = 1.25         # over-select before collapse so dedup doesn't starve


def half_life_days(importance: int) -> float:
    """22.5d at importance 1 → 52.5d at 5 → 82.5d at 9 (~4× spread; FadeMem's
    'important memories decay 3–5× slower' — mechanism verified, numbers not).
    Importance 10 is pinned: infinite half-life, decay never touches it —
    over a years-long horizon every finite multiplier reaches zero, so
    permanence has to be a tier, not a stretch."""
    if importance >= PINNED:
        return math.inf
    return HALF_LIFE_BASE_DAYS * (0.5 + importance / 4.0)


def score(fact: dict, now: float) -> float:
    imp = fact.get("importance") or DEFAULT_IMPORTANCE
    age_days = max(0.0, now - (fact.get("event_date") or 0)) / 86400.0
    return (imp / 10.0) * math.exp(-age_days / half_life_days(imp))


def _collapse(facts: list[dict]) -> list[dict]:
    """Drop exact (hash) and paraphrase (cosine) duplicates, keeping the first
    occurrence — callers order the list so the preferred copy comes first."""
    kept, kept_vecs, seen_hash = [], [], set()
    for f in facts:
        h = f.get("content_hash")
        if h and h in seen_hash:
            continue
        v = embeddings.unpack(f["embedding"]) if f.get("embedding") else None
        if v is not None and any(
                embeddings.cosine(v, kv) > PARAPHRASE_CUTOFF for kv in kept_vecs):
            continue
        kept.append(f)
        if h:
            seen_hash.add(h)
        if v is not None:
            kept_vecs.append(v)
    return kept


def select_for_summary(con, now: float | None = None) -> tuple[list[dict], list[dict]]:
    """(durable, active) — each sorted oldest-first for the summary prompt's
    'later = more recent' convention."""
    now = now or db.now()
    # origin_agent + source travel through selection so the summary prompt (and
    # the /summary response's structured provenance) can carry each fact's real
    # recorded origin — coarse provenance, never a guessed speaker.
    rows = [dict(r) for r in con.execute(
        "SELECT id, content, importance, event_date, created_at, content_hash, "
        "embedding, origin_agent, source FROM facts "
        "WHERE invalidated_at IS NULL AND quarantined_at IS NULL")]

    def imp(f):
        return f.get("importance") or DEFAULT_IMPORTANCE

    # Pinned facts (importance 10) are unconditionally durable and do NOT
    # consume the pool's 200 slots — permanence must not be a competition a
    # decade of accumulated 8s and 9s can eventually win.
    pinned = [f for f in rows if imp(f) >= PINNED]
    rest = [f for f in rows if imp(f) < PINNED]
    durable_cand = pinned + sorted(
        rest, key=lambda f: (imp(f), f["event_date"] or 0),
        reverse=True)[:int(DURABLE_N * POOL_MARGIN)]

    # Settle the durable pool BEFORE choosing the active one, so the active pool
    # competes over everything durable did not actually take.
    #
    # This used to exclude all 250 over-selected CANDIDATES from the
    # active pool while folding only 200 of them back, so the ~50 the margin
    # trimmed landed in NEITHER pool — a silent hole running from ~200 valid
    # facts to ~550, a range a mature ledger reaches quickly. The margin exists
    # to survive dedup, not to drop cards on the floor.
    durable = _collapse(durable_cand)[:DURABLE_N + len(pinned)]
    durable_ids = {f["id"] for f in durable}
    active_cand = sorted((f for f in rows if f["id"] not in durable_ids),
                         key=lambda f: score(f, now),
                         reverse=True)[:int(ACTIVE_N * POOL_MARGIN)]

    # one collapse across both pools, durable first so the durable copy wins
    active = [f for f in _collapse(durable + active_cand)
              if f["id"] not in durable_ids][:ACTIVE_N]
    key = lambda f: (f["event_date"] or 0, f["id"])  # noqa: E731
    return sorted(durable, key=key), sorted(active, key=key)
