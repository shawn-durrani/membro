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
