"""Scheduled backups — a long-running instance must not sit on a stale
restore point (backups used to happen only at startup / on manual request)."""

import os
import threading
import time

from memory_service import db as db_mod
from memory_service.config import Settings


def _snaps(settings):
    return sorted((settings.data_dir / "backups").glob("memory-*.db"))


HOUR = 3600.0
DAY = 24 * HOUR


def _write(con, external_id):
    con.execute("INSERT INTO conversations(source_app, external_id, created_at) "
                "VALUES ('t', ?, 1700000000.0)", (external_id,))
    con.commit()


def _set_mtime(path, t):
    os.utime(path, (t, t))


def _standing_snapshot(settings, taken_at):
    """A restore point taken at `taken_at`, renamed to an older stamp so a
    fresh snapshot in the same second can't land on its file name."""
    snap = db_mod.backup(settings)
    old = snap.with_name("memory-20200101-000000.db")
    snap.rename(old)
    _set_mtime(old, taken_at)
    return old


def _set_db_mtime(settings, t):
    for p in (settings.db_path, settings.db_path.with_suffix(".db-wal")):
        if p.exists():
            _set_mtime(p, t)


def _scheduler_thread(before):
    (thread,) = [t for t in threading.enumerate()
                 if t.name == "backup-scheduler" and t not in before]
    return thread


def test_an_overdue_snapshot_is_taken_on_the_first_tick_after_sleep(settings, con):
    """The Mac sleeps for three days: the wall clock jumps and awake time
    barely moves. The first tick after waking takes the overdue snapshot."""
    t0 = time.time()
    _standing_snapshot(settings, t0 - HOUR)
    _write(con, "c1")                                  # memory changed after it
    last = db_mod._scheduled_tick(settings, t0 + 300, t0)
    assert len(_snaps(settings)) == 1                  # five minutes on: not due
    last = db_mod._scheduled_tick(settings, t0 + 3 * DAY, last)
    assert len(_snaps(settings)) == 2
    assert last == t0 + 3 * DAY


def test_a_snapshot_that_is_not_overdue_is_not_taken(settings, con):
    t0 = time.time()
    _standing_snapshot(settings, t0 - HOUR)
    _write(con, "c1")
    last = db_mod._scheduled_tick(settings, t0 + 5 * HOUR - 1, None)
    assert last is None                                # no try was made
    assert len(_snaps(settings)) == 1


def test_an_unchanged_database_takes_no_snapshot(settings, con):
    t0 = time.time()
    _write(con, "c1")
    _standing_snapshot(settings, t0 - DAY)
    _set_db_mtime(settings, t0 - 2 * DAY)              # last written before it
    last = db_mod._scheduled_tick(settings, t0, None)
    assert last is None
    assert len(_snaps(settings)) == 1


def test_a_deduped_try_waits_a_full_interval(settings, con, monkeypatch):
    """A touched but byte-identical DB is copied, found identical and
    dropped, so the old snapshot stands. The try itself restarts the
    interval, or every tick would copy the whole DB again."""
    t0 = time.time()
    _standing_snapshot(settings, t0 - DAY)
    _set_db_mtime(settings, t0)                        # mtime moved, content not
    last = db_mod._scheduled_tick(settings, t0, None)
    assert last == t0
    assert len(_snaps(settings)) == 1                  # identical copy dropped
    calls = []
    monkeypatch.setattr(db_mod, "backup", calls.append)
    assert db_mod._scheduled_tick(settings, t0 + 300, last) == t0
    assert calls == []
    db_mod._scheduled_tick(settings, t0 + 6 * HOUR, last)
    assert len(calls) == 1


def test_times_ahead_of_the_clock_are_ignored(settings, con):
    """A clock set back must not stall backups until it catches up."""
    t0 = time.time()
    _standing_snapshot(settings, t0 + 30 * DAY)
    assert db_mod._snapshot_due(settings, t0, t0 + 30 * DAY)
    assert not db_mod._snapshot_due(settings, t0, t0 - HOUR)


def test_the_scheduler_runs_on_the_wall_clock(settings, con, monkeypatch):
    """The loop waits on the monotonic clock, so a jump in the injected wall
    clock alone must be enough for the next tick to snapshot."""
    t0 = time.time()
    _standing_snapshot(settings, t0 - HOUR)
    _write(con, "c1")
    wall, reads = [t0], []
    ticking, took = threading.Event(), threading.Event()

    def clock():
        reads.append(wall[0])
        if len(reads) >= 3:                            # the seed, then two ticks
            ticking.set()
        return wall[0]

    monkeypatch.setattr(db_mod, "backup", lambda s: took.set())
    before = threading.enumerate()
    stop = db_mod.start_backup_scheduler(settings, clock=clock, tick_s=0.01)
    try:
        assert ticking.wait(5)
        assert not took.is_set()                       # awake, nothing due yet
        wall[0] = t0 + 3 * DAY                         # asleep for three days
        assert took.wait(5)
    finally:
        stop.set()
    _scheduler_thread(before).join(5)


def test_scheduler_disabled_by_nonpositive_interval(settings, con):
    stop = db_mod.start_backup_scheduler(
        settings.model_copy(update={"backup_interval_hours": 0}))
    assert not stop.is_set()  # returned, no thread; setting it is still safe
    stop.set()


def test_stop_event_halts_the_loop_promptly(settings, con):
    before = threading.enumerate()
    stop = db_mod.start_backup_scheduler(settings)     # the five-minute tick
    thread = _scheduler_thread(before)
    stop.set()
    thread.join(2)
    assert not thread.is_alive()


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
