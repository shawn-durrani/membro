"""One-off repair for #195: lift the low mark from facts the owner approved.

Two holds mark a fact low confidence as they hold it: the write gate, and
the miner's checks. Until #195, approving such a fact cleared the hold and
kept the low mark, so recall went on handing a fact the owner had vouched
for to every AI as doubtful. Approval now sets high. This script does the
same for facts approved before that, on the owner's rule of 2 October:
only facts membro's own log shows the owner approved, and never edited.

The evidence is the service log, data/service.log, with service.log.1 read
first when it's there. A fact counts as approved when the log holds a
`POST /v1/facts/<id>/approve` that answered 200. A bulk approve names no ids
in the log, so it's no evidence. The access lines carry no time, so the
order of lines is the only clock, and the dry run says how far back the
log reaches.

An approved fact is raised only when all of these hold:

- it's still in the ledger, current, and not held again,
- the judge didn't release it later, which leaves a note as its reason,
- the log shows no `PATCH /v1/facts/<id>` that answered 200, before the
  approval or after it, so the owner never edited it by hand,
- it's low now,
- the low came from a hold that lowers it: the fact was mined, because
  the miner marks a fact low only when it holds it, or it came over MCP,
  which the write gate always holds. A low on any other fact may be the
  confidence it was saved with, and approval keeps that.

Each one is raised to what approval now sets, ledger.UNHELD_CONFIDENCE.

Dry run by default: each fact it would raise prints as its id, never any
text, and the approved facts it leaves alone are counted by reason. With
--apply it refuses while membro is busy, saves a copy of memory.db in data/
first, raises every fact in one transaction, and appends one content-free
line per fact to data/repairs.jsonl. It changes nothing but confidence.

Run:  .venv/bin/python scripts/raise_approved_confidence.py [--apply]
          [--data-dir PATH] [--log PATH ...]
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_service import db, ledger  # noqa: E402
from memory_service.config import load_settings  # noqa: E402

REPAIR = "raise-approved-confidence-195"
JOURNAL_NAME = "repairs.jsonl"
LOG_NAMES = ("service.log.1", "service.log")   # oldest first

# uvicorn's access line: `INFO:  127.0.0.1:5 - "POST /v1/facts/12/approve
# HTTP/1.1" 200 OK`. A query string, if any, is allowed and ignored.
APPROVE = re.compile(r'"POST /v1/facts/(\d+)/approve(?:\?\S*)? HTTP/[\d.]+" 200\b')
EDIT = re.compile(r'"PATCH /v1/facts/(\d+)(?:\?\S*)? HTTP/[\d.]+" 200\b')
DATED = re.compile(r"^(\d{4}-\d\d-\d\d)[ T]\d\d:\d\d")

MISSING = "no longer in the ledger"
SUPERSEDED = "superseded since"
HELD = "held again"
JUDGE = "released by the judge since"
EDITED_AFTER = "edited by hand after it was approved"
EDITED_BEFORE = "edited by hand before it was approved"
NOT_LOW = "not low"
KEPT = "its low may be the confidence it was saved with"
SKIPS = (MISSING, SUPERSEDED, HELD, JUDGE, EDITED_AFTER, EDITED_BEFORE,
         NOT_LOW, KEPT)


def read_log(paths: list[Path]) -> dict:
    """What the log says, in line order across the files given: for each
    fact id, the line numbers of its 200 approvals and its 200 edits, plus
    how far back the log reaches. Only ids and line numbers are kept."""
    approvals: dict[int, list[int]] = {}
    edits: dict[int, list[int]] = {}
    files, first_dated, n = [], None, 0
    for path in paths:
        if not path.is_file():
            continue
        st = path.stat()
        born = getattr(st, "st_birthtime", None)
        lines = 0
        with open(path, errors="replace") as f:
            for line in f:
                n += 1
                lines += 1
                if (m := APPROVE.search(line)):
                    approvals.setdefault(int(m.group(1)), []).append(n)
                elif (m := EDIT.search(line)):
                    edits.setdefault(int(m.group(1)), []).append(n)
                elif first_dated is None and (m := DATED.match(line)):
                    first_dated = m.group(1)
        files.append({"name": path.name, "lines": lines,
                      "created": (datetime.date.fromtimestamp(born).isoformat()
                                  if born else None)})
    return {"approvals": approvals, "edits": edits, "files": files,
            "first_dated": first_dated,
            "approval_lines": sum(len(v) for v in approvals.values())}


def plan(con, log: dict) -> tuple[list[dict], Counter]:
    """Every approved fact to raise, by id, plus the approved facts left
    alone counted by the first reason that applies. Text is never read."""
    approved = sorted(log["approvals"])
    rows = {}
    if approved:
        rows = {r["id"]: dict(r) for r in con.execute(
            "SELECT id, source, origin_agent, confidence, invalidated_at, "
            "quarantined_at, quarantine_reason FROM facts "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(approved),))}
    raise_, skipped = [], Counter()
    for fid in approved:
        f = rows.get(fid)
        last_approval = log["approvals"][fid][-1]
        first_approval = log["approvals"][fid][0]
        edits = log["edits"].get(fid, [])
        if f is None:
            skipped[MISSING] += 1
        elif f["invalidated_at"] is not None:
            skipped[SUPERSEDED] += 1
        elif f["quarantined_at"] is not None:
            skipped[HELD] += 1
        elif f["quarantine_reason"] is not None:
            # approve clears the reason; the judge's release writes a note
            skipped[JUDGE] += 1
        elif any(e > first_approval for e in edits):
            skipped[EDITED_AFTER] += 1
        elif edits:
            skipped[EDITED_BEFORE] += 1
        elif f["confidence"] != "low":
            skipped[NOT_LOW] += 1
        elif not (f["source"] == "chat"
                  or (f["origin_agent"] or "").startswith("mcp:")):
            skipped[KEPT] += 1
        else:
            raise_.append({"fact_id": fid, "source": f["source"],
                           "approvals": len(log["approvals"][fid]),
                           "last_approval_line": last_approval})
    return raise_, skipped


def describe(r: dict) -> str:
    return f"fact {r['fact_id']}  {r['source']}  approved {r['approvals']}x"


def reach(log: dict) -> str:
    parts = []
    for f in log["files"]:
        made = f" (file created {f['created']})" if f["created"] else ""
        parts.append(f"{f['name']}: {f['lines']} lines{made}")
    dated = (f"; its first dated line is {log['first_dated']}, and the "
             "request lines carry no time" if log["first_dated"] else
             "; no line in it carries a date")
    return ", ".join(parts) + dated


def running_work(settings):
    """membro's busy reasons, [] when idle, or None when nothing answers on
    its port. Any other answer raises: an --apply that can't tell whether
    membro is mid-work doesn't go ahead."""
    url = f"http://{settings.host}:{settings.port}/v1/busy"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return list(json.load(resp).get("reasons") or [])
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            return None
        raise


def snapshot(settings) -> Path:
    """A consistent copy of memory.db in data/, taken with SQLite's online
    backup like the service's own snapshots, owner-only. It sits outside
    data/backups, so the snapshot rotation never removes it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = settings.data_dir / f"memory-before-confidence-{stamp}.db"
    n = 1
    while dest.exists():
        n += 1
        dest = settings.data_dir / f"memory-before-confidence-{stamp}-{n}.db"
    src = db.connect(settings.db_path)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    os.chmod(dest, 0o600)
    return dest


def apply(con, log: dict, raises: list[dict], backup: Path) -> None:
    """Raise every fact in one transaction, each re-checked in the update
    itself, and journal each change without any text."""
    ids = [r["fact_id"] for r in raises]
    held = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        kept = held.execute(
            "SELECT count(*) FROM facts WHERE confidence='low' "
            "AND id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),)).fetchone()[0]
    finally:
        held.close()
    if kept != len(ids):
        raise SystemExit(f"the copy {backup} doesn't hold every fact low; "
                         "nothing changed")
    again, _ = plan(con, log)
    if again != raises:
        raise SystemExit("the facts changed since the dry run; nothing "
                         "changed, run it again")
    try:
        for r in raises:
            cur = con.execute(
                "UPDATE facts SET confidence=? WHERE id=? AND confidence='low' "
                "AND quarantined_at IS NULL AND invalidated_at IS NULL "
                "AND quarantine_reason IS NULL",
                (ledger.UNHELD_CONFIDENCE, r["fact_id"]))
            if cur.rowcount != 1:
                raise SystemExit(f"fact {r['fact_id']} changed while raising; "
                                 "nothing changed, run it again")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    at = time.time()
    with open(backup.parent / JOURNAL_NAME, "a") as journal:
        for r in raises:
            journal.write(json.dumps({
                "at": at, "repair": REPAIR, "action": "raised",
                "fact_id": r["fact_id"], "old_confidence": "low",
                "new_confidence": ledger.UNHELD_CONFIDENCE,
                "approvals_logged": r["approvals"],
                "backup": backup.name}) + "\n")


def main(argv=None, settings=None, probe=running_work, out=print) -> int:
    ap = argparse.ArgumentParser(
        description="Lift the low mark from facts the owner approved (#195).")
    ap.add_argument("--apply", action="store_true",
                    help="raise the facts; the default is a dry run")
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--log", type=Path, action="append", default=None,
                    help="a service log to read, oldest first; repeat for "
                         "more (default: service.log.1 then service.log "
                         "in the data folder)")
    args = ap.parse_args(argv)

    settings = settings or load_settings()
    if args.data_dir:
        settings = settings.model_copy(
            update={"data_dir": args.data_dir.resolve()})
    if not settings.db_path.exists():
        # db.connect() would create an empty database here.
        raise SystemExit(f"no database at {settings.db_path}")
    logs = args.log or [settings.data_dir / name for name in LOG_NAMES]
    log = read_log(logs)
    if not log["files"]:
        raise SystemExit("no service log to read: "
                         + ", ".join(str(p) for p in logs))

    mode = "APPLY" if args.apply else "dry-run"
    con = db.connect(settings.db_path)
    try:
        raises, skipped = plan(con, log)
        out(f"[{mode}] read {reach(log)}")
        for r in raises:
            out(f"[{mode}] {describe(r)}")
        out(f"[{mode}] {len(log['approvals'])} fact(s) have a logged approval "
            f"({log['approval_lines']} approve line(s)): {len(raises)} to "
            f"raise to {ledger.UNHELD_CONFIDENCE}, {sum(skipped.values())} "
            "left alone")
        for reason in SKIPS:
            if skipped[reason]:
                out(f"[{mode}]   left alone, {reason}: {skipped[reason]}")
        if not raises:
            out(f"[{mode}] nothing to raise; nothing to do")
            return 0
        if not args.apply:
            out("[dry-run] nothing changed; run again with --apply")
            return 0

        try:
            work = probe(settings)
        except Exception as e:
            raise SystemExit(f"couldn't ask membro on :{settings.port} "
                             f"whether it's busy ({type(e).__name__}); "
                             "nothing changed")
        if work:
            raise SystemExit(f"membro is busy ({', '.join(work)}); nothing "
                             "changed, run it again when it's idle")
        # The copy and the journal sit beside the memory: owner-only.
        db.secure_data_dir(settings)
        backup = snapshot(settings)
        out(f"[APPLY] copy saved: {backup}")
        apply(con, log, raises, backup)
        out(f"[APPLY] raised {len(raises)} fact(s); journal: "
            f"{settings.data_dir / JOURNAL_NAME}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
