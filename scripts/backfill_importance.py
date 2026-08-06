"""One-off: score importance (1-9) for facts left unscored in the ledger.

Two populations land here: facts that predate extraction-time scoring (an
imported legacy ledger) and — the reason this matters now — the several hundred facts the
miner stored with NULL importance while it was silently dropping the
importance= tag (#69). Both are invisible to summary selection in the same way:
weighting reads a NULL as a neutral 5, so a genuinely important recent fact
loses its profile slot to older facts carrying explicit high scores.

Writes ONLY the empty `importance` column — never content, dates, or state.
Only rows with importance IS NULL are touched, so re-runs are no-ops and
facts scored at extraction are never overwritten. Uses the same anchored scale
the miner uses AND the same 1-9 cap: importance 10 means PERMANENT and is
owner-only, so a batch scoring pass must never be able to mint it (mirrors the
miner's cap for the identical reason).

Usage:
  .venv/bin/python scripts/backfill_importance.py [--data-dir PATH]
      [--limit N]   # score only the first N unscored facts (sanity sample)
      [--dry-run]   # print scores, write nothing

Run --dry-run first against the live data dir to preview scores; the writing
run needs write access to the live ledger, so on a shared/production DB leave
the actual write to the ledger's owner.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_service import db, llm  # noqa: E402
from memory_service.config import load_settings  # noqa: E402

BATCH = 40
_SCORE_RE = re.compile(r"^\s*(\d+)\s*[=:]\s*(\d{1,2})\s*$")

PROMPT = (
    "You are scoring entries from a permanent memory ledger about {user} for "
    "long-term importance on a 1-9 scale. importance is how much the fact "
    "matters to understanding the person long-term, NOT how often it came up "
    "or how technical it is. Anchors: 1-2 = mundane detail (a tool preference, "
    "a one-off errand); 4-6 = notable (a project decision, a stated goal, a "
    "colleague's role); 8-9 = life-defining (a new job, a move, family, "
    "health). Most facts are 3-6; reserve 8-9 for genuinely major items. Never "
    "assign 10 — it is reserved for the owner to mark facts as permanent.\n"
    "Output one line per entry, exactly `<id>=<score>`, nothing else.\n\n"
    "## Entries\n{entries}"
)


def parse_scores(out: str) -> dict[int, int]:
    scores = {}
    for line in out.splitlines():
        m = _SCORE_RE.match(line)
        if m:
            # Cap at 9: importance 10 is owner-only PERMANENT — a batch scoring
            # pass must never mint permanence (mirrors the miner's cap, #69).
            scores[int(m.group(1))] = min(9, max(1, int(m.group(2))))
    return scores


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    if args.data_dir:
        settings = settings.model_copy(update={"data_dir": args.data_dir.resolve()})
    con = db.connect(settings.db_path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, content FROM facts WHERE importance IS NULL ORDER BY id")]
        if args.limit:
            rows = rows[:args.limit]
        print(f"{len(rows)} unscored facts in {settings.db_path}")
        scored = skipped = 0
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            entries = "\n".join(f"{f['id']} | {f['content']}" for f in batch)
            out = llm.utility_complete(
                PROMPT.format(user=settings.user_name, entries=entries),
                settings, max_tokens=1000)
            scores = parse_scores(out or "")
            wanted = {f["id"] for f in batch}
            for fid in wanted:
                if fid not in scores:
                    skipped += 1  # unparsed → stays NULL, safe to re-run
                    continue
                if not args.dry_run:
                    con.execute(
                        "UPDATE facts SET importance=? WHERE id=? "
                        "AND importance IS NULL", (scores[fid], fid))
                scored += 1
            if not args.dry_run:
                con.commit()
            print(f"  batch {i // BATCH + 1}: {len(wanted & set(scores))} scored",
                  flush=True)
        print(f"{'DRY-RUN: would score' if args.dry_run else 'scored'} {scored}, "
              f"unparsed-skipped {skipped} (remain NULL; re-run to retry)")
        if args.dry_run and rows:
            sample = [dict(r) for r in con.execute(
                "SELECT id, content FROM facts WHERE importance IS NULL "
                "ORDER BY id LIMIT 5")]
            for f in sample:
                print(f"  e.g. [{f['id']}] {f['content'][:70]}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
