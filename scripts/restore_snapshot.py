"""Restore a membro snapshot and replay the erasures made since (#101).

Usage:
  .venv/bin/python scripts/restore_snapshot.py data/backups/memory-YYYYMMDD-HHMMSS.db
  .venv/bin/python scripts/restore_snapshot.py --list
  .venv/bin/python scripts/restore_snapshot.py SNAPSHOT --dry-run

Stop the service first; the command refuses while membro answers on its
port. It snapshots the live database before touching it, so the step can be
undone by restoring that copy. Output is counts and paths only.

The fleet runbook (workbench runbooks/membro-restore.md) covers the whole
procedure, including winding crossband's ingest mark back afterwards.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_service import restore as restore_mod  # noqa: E402
from memory_service.config import load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot", nargs="?", type=Path)
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--list", action="store_true", help="list snapshots and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what a restore would replay; change nothing")
    args = ap.parse_args()
    settings = load_settings()
    if args.data_dir:
        settings.data_dir = args.data_dir
    if args.list:
        for p in sorted((settings.data_dir / "backups").glob("memory-*.db")):
            print(p)
        return 0
    if not args.snapshot:
        ap.error("a snapshot path is required (or --list)")
    try:
        result = restore_mod.restore(settings, args.snapshot, dry_run=args.dry_run)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
