"""Restore a snapshot without undoing the owner's erasures (#101).

A snapshot is a point in time, and the erasure journal lives inside the
database, so copying an older snapshot over the live file would bring back
every fact, message and attachment erased since, with the tombstones that
recorded them gone too. `restore()` keeps the promise: it snapshots the live
database first, reads the live journal, copies the chosen snapshot into
place, migrates it to the current schema, then replays every journalled
erasure the restored copy does not carry, through the same erasers the
owner's hand uses. The replayed tombstones are appended under their original
times, so the journal stays a complete record.

Run it with the service stopped. The command refuses while the service
answers on its port, and it never prints content, only counts and paths.
"""

import os
import re
import shutil
import sqlite3
import time
import urllib.request
from pathlib import Path

from . import db, erasers

_REF_ID = re.compile(r"^(fact|attachment|message):(\d+)")


def service_answers(settings, timeout: float = 1.0) -> bool:
    """True when something answers /v1/busy on the configured port. The
    restore must not run under a live service: the copy would race the WAL
    and the replay would race the miner."""
    url = f"http://{settings.host}:{settings.port}/v1/busy"
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 - any refusal means nothing is listening
        return False


def read_journal(path: Path) -> list[dict]:
    con = db.connect(path)
    try:
        has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='erasures'").fetchone()
        if not has:
            return []
        return [dict(r) for r in con.execute(
            "SELECT id, ts, kind, ref FROM erasures ORDER BY id")]
    finally:
        con.close()


def missing_erasures(live: list[dict], restored: list[dict]) -> list[dict]:
    """Journal rows the live copy carries that the restored copy lacks, by
    (kind, ref, ts): ids are not compared, because a lineage that was itself
    restored once may number its journal differently."""
    have = {(r["kind"], r["ref"], r["ts"]) for r in restored}
    return [r for r in live if (r["kind"], r["ref"], r["ts"]) not in have]


def replay(con, settings, rows: list[dict]) -> dict:
    """Apply each missing tombstone to the restored database. A row the
    restored copy never had (created and erased after the snapshot) is
    counted as already absent and still journalled, so the record is whole."""
    counts = {"fact": 0, "attachment": 0, "message": 0, "already_absent": 0}
    for r in rows:
        m = _REF_ID.match(r["ref"])
        if not m:
            db.journal_erasure(con, r["kind"], r["ref"], ts=r["ts"])
            con.commit()
            counts["already_absent"] += 1
            continue
        kind, row_id = m.group(1), int(m.group(2))
        if kind == "fact":
            res = erasers.erase_fact(con, row_id, journal_ts=r["ts"])
        elif kind == "attachment":
            res = erasers.erase_attachment(con, settings, row_id, journal_ts=r["ts"])
        else:
            res = erasers.erase_message(con, row_id, journal_ts=r["ts"])
        if res is None:
            db.journal_erasure(con, r["kind"], r["ref"], ts=r["ts"])
            con.commit()
            counts["already_absent"] += 1
        else:
            counts[kind] += 1
    return counts


def _check_snapshot(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"no such snapshot: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA quick_check").fetchone()[0]
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    if ok != "ok":
        raise ValueError(f"snapshot fails quick_check: {ok}")
    if "facts" not in tables or "messages" not in tables:
        raise ValueError("snapshot is not a membro database")


def _pre_restore_copy(settings) -> Path:
    """A consistent copy of the live database under its own name, so the
    restore can be undone. Not db.backup(): that names snapshots to the
    second and would overwrite a snapshot taken moments earlier, which may
    be the very one being restored."""
    bdir = settings.data_dir / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"memory-{stamp}-pre-restore.db"
    n = 1
    while dest.exists():
        n += 1
        dest = bdir / f"memory-{stamp}-pre-restore-{n}.db"
    src = db.connect(settings.db_path)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    return dest


def restore(settings, snapshot: Path, dry_run: bool = False) -> dict:
    snapshot = Path(snapshot)
    _check_snapshot(snapshot)
    if not settings.db_path.exists():
        raise FileNotFoundError(f"no live database at {settings.db_path}")
    if service_answers(settings):
        raise RuntimeError(
            f"membro is answering on port {settings.port}; stop the service first "
            "(launchctl bootout gui/$(id -u)/dev.membro.server)")
    live_journal = read_journal(settings.db_path)
    snap_journal = read_journal(snapshot)
    missing = missing_erasures(live_journal, snap_journal)
    result = {
        "snapshot": str(snapshot),
        "live_journal_rows": len(live_journal),
        "snapshot_journal_rows": len(snap_journal),
        "to_replay": len(missing),
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    # Work from a private copy of the chosen snapshot: the startup backup
    # that db.init takes below names files to the second and could
    # otherwise land on top of the source.
    work = settings.data_dir / f".restore-{snapshot.name}"
    shutil.copy2(snapshot, work)
    try:
        pre = _pre_restore_copy(settings)
        result["pre_restore_snapshot"] = str(pre)
        for suffix in (".db-wal", ".db-shm"):
            settings.db_path.with_suffix(suffix).unlink(missing_ok=True)
        shutil.copy2(work, settings.db_path)
        os.chmod(settings.db_path, 0o600)
    finally:
        work.unlink(missing_ok=True)

    db.init(settings)  # migrate the older copy to the current schema
    con = db.connect(settings.db_path)
    try:
        result["replayed"] = replay(con, settings, missing)
        result["fts"] = db.repair_fts(con)
    finally:
        con.close()
    return result
