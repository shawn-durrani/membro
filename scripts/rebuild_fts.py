"""Manual trigger for the FTS5 desync repair that also runs automatically on
every db.init() (service startup).

Context: messages_fts / attachments_fts are external-content FTS5 indexes,
kept current only by an AFTER INSERT trigger on their base table. Nothing
backfills them from existing rows. If the virtual table is ever dropped and
recreated (schema experiment, corruption recovery, a partial restore that
copied the base table but not its FTS shadow tables), the index goes stale
forever: MATCH queries succeed but find nothing, silently, with no error —
search_history / POST /v1/search return zero results for real data even for
generic terms, while everything else (recall, the fact ledger) is unaffected.

This script is a standalone way to check/fix that on an already-running
instance without waiting for (or forcing) a restart. It is safe to run any
number of times: `fts_status` only rebuilds a table when its row count
disagrees with its base table, and `INSERT INTO <fts>(<fts>) VALUES('rebuild')`
touches only the derived index, never `messages`/`attachments` themselves.

Usage:
  .venv/bin/python scripts/rebuild_fts.py [--data-dir PATH] [--check]

  --check   report status only, make no changes (exit 1 if anything is
            out of sync, for use in a monitoring/cron check)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_service import db  # noqa: E402
from memory_service.config import load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--check", action="store_true",
                    help="report only, make no changes")
    args = ap.parse_args()

    settings = load_settings()
    if args.data_dir:
        settings = settings.model_copy(update={"data_dir": args.data_dir.resolve()})

    con = db.connect(settings.db_path)
    try:
        status = db.fts_status(con)
        if not status:
            print(f"no FTS tables found in {settings.db_path}")
            return 0
        out_of_sync = [fts for fts, s in status.items() if not s["in_sync"]]
        for fts, s in status.items():
            mark = "OK" if s["in_sync"] else "OUT OF SYNC"
            print(f"  {fts}: {s['fts_rows']} indexed rows vs "
                  f"{s['base_rows']} in {s['base_table']} — {mark}")
        if not out_of_sync:
            print("all FTS indexes in sync; nothing to do")
            return 0
        if args.check:
            print(f"OUT OF SYNC: {out_of_sync} (re-run without --check to repair)")
            return 1
        result = db.repair_fts(con)
        print(f"rebuilt: {result['repaired']}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
