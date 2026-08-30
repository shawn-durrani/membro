"""Scheduled backups — a long-running instance must not sit on a stale
restore point (backups used to happen only at startup / on manual request)."""

import time

from memory_service import db as db_mod
from memory_service.config import Settings


def _snaps(settings):
    return sorted((settings.data_dir / "backups").glob("memory-*.db"))


def _wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_scheduler_snapshots_after_a_change(settings, con):
    baseline = len(_snaps(settings))
    con.execute("INSERT INTO conversations(source_app, external_id, created_at) "
                "VALUES ('t', 'c1', 1700000000.0)")
    con.commit()
    stop = db_mod.start_backup_scheduler(
        settings.model_copy(update={"backup_interval_hours": 0.05 / 3600}))
    try:
        assert _wait_for(lambda: len(_snaps(settings)) > baseline)
    finally:
        stop.set()


def test_scheduler_skips_when_nothing_changed(settings, con):
    db_mod.backup(settings)  # fresh snapshot newer than any DB write
    baseline = len(_snaps(settings))
    stop = db_mod.start_backup_scheduler(
        settings.model_copy(update={"backup_interval_hours": 0.05 / 3600}))
    try:
        time.sleep(0.4)  # several ticks
        assert len(_snaps(settings)) == baseline
    finally:
        stop.set()


def test_scheduler_disabled_by_nonpositive_interval(settings, con):
    stop = db_mod.start_backup_scheduler(
        settings.model_copy(update={"backup_interval_hours": 0}))
    assert not stop.is_set()  # returned, no thread; setting it is still safe
    stop.set()


def test_stop_event_halts_the_loop(settings, con):
    stop = db_mod.start_backup_scheduler(
        settings.model_copy(update={"backup_interval_hours": 0.05 / 3600}))
    stop.set()
    time.sleep(0.15)
    baseline = len(_snaps(settings))
    con.execute("INSERT INTO conversations(source_app, external_id, created_at) "
                "VALUES ('t', 'c2', 1700000001.0)")
    con.commit()
    time.sleep(0.3)
    assert len(_snaps(settings)) == baseline


def test_identical_content_takes_no_new_snapshot(settings, con):
    first = db_mod.backup(settings)
    time.sleep(0.05)
    again = db_mod.backup(settings)
    assert again == first          # the standing snapshot is the answer
    assert len(_snaps(settings)) == 1


def test_a_crash_loop_no_longer_shreds_the_history(settings, con):
    """init snapshots pre-migration on every startup, retention keeps the
    newest backup_keep copies by count, and launchd restarts a crashing
    service every ~10 seconds - so backup_keep restarts (about two
    minutes) used to evict the whole pre-crash history at exactly the
    moment it was needed. Repeated init with nothing changing must not
    add, evict, or rewrite a snapshot."""
    con.execute("INSERT INTO conversations(source_app, external_id, created_at) "
                "VALUES ('t', 'c1', 1700000000.0)")
    con.commit()
    db_mod.init(settings)          # one changed startup takes its snapshot
    before = [(f.name, f.stat().st_mtime_ns) for f in _snaps(settings)]
    assert before
    time.sleep(0.05)
    for _ in range(5):             # the crash loop
        db_mod.init(settings)
    after = [(f.name, f.stat().st_mtime_ns) for f in _snaps(settings)]
    assert after == before
