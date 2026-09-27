"""The data directory is owner-only (#133).

A Mac home folder is readable by the `staff` group, which every local
account is in. The database was 0600, but every backup was 0644, and a
backup copied out of `data/` keeps its own mode. Startup now takes group
and other access off whatever is already there, and every file made after
it is 0600 from the start, folders 0700. Keyless, on a throwaway data
directory, starting from the common 0o022 umask.
"""

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from memory_service import db as mdb
from memory_service import episodic, restore


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def loose_file(path: Path, body: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.chmod(path, 0o644)
    return path


@pytest.fixture(autouse=True)
def default_umask():
    """Start each test where a fresh launchd or shell process starts."""
    os.umask(0o022)


def test_tightens_what_is_already_there(settings, tmp_path):
    data = settings.data_dir
    data.mkdir()
    os.chmod(data, 0o755)
    loose = [
        loose_file(settings.db_path),
        loose_file(data / "memory.db-wal", b""),
        loose_file(data / "memory.db-shm", b""),
        loose_file(data / "backups" / "memory-20260101-000000.db"),
        loose_file(data / "backups" / "memory-20260101-000000.db-shm"),
        loose_file(data / "attachments" / "abc.txt"),
        loose_file(data / "service.log"),
    ]
    folders = [data / "backups", data / "attachments"]
    for d in folders:
        os.chmod(d, 0o755)
    outside = loose_file(tmp_path / "elsewhere.txt")
    (data / "link.txt").symlink_to(outside)

    assert mdb.secure_data_dir(settings) == len(loose) + len(folders) + 1

    assert mode(data) == 0o700
    for f in loose:
        assert mode(f) == 0o600, f
    for d in folders:
        assert mode(d) == 0o700, d
    # A symlink out of the data directory, and what it points at, are left
    # alone: chmod would follow it.
    assert mode(outside) == 0o644
    assert mdb.secure_data_dir(settings) == 0  # nothing left to tighten


def test_startup_runs_it_before_the_first_snapshot(settings):
    """init() snapshots the database before it migrates, so the pass has to
    come first or the snapshot copies an open mode."""
    data = settings.data_dir
    data.mkdir()
    os.chmod(data, 0o755)
    sqlite3.connect(settings.db_path).close()
    os.chmod(settings.db_path, 0o644)
    log = loose_file(data / "service.log")

    mdb.init(settings)

    assert mode(data) == 0o700
    assert mode(settings.db_path) == 0o600
    assert mode(log) == 0o600
    [snapshot] = (data / "backups").glob("memory-*.db")
    assert mode(snapshot) == 0o600
    assert mode(data / "backups") == 0o700


def test_files_made_after_startup_are_owner_only(settings, tmp_path):
    settings.mirror_dir = tmp_path / "mirror"
    mdb.init(settings)  # a first run: nothing exists yet
    con = mdb.connect(settings.db_path)
    try:
        episodic.ingest(con, "multi-model-chat", "chat-1", [
            {"external_id": "m1", "speaker": "user", "content": "hello",
             "created_at": 1700000000.0}], title="t")
        con.commit()
        # WAL mode: the -wal and -shm files exist while a connection is open.
        assert mode(settings.db_path.with_suffix(".db-wal")) == 0o600
        assert mode(settings.db_path.with_suffix(".db-shm")) == 0o600
    finally:
        con.close()

    snapshot = mdb.backup(settings)
    assert mode(snapshot) == 0o600
    assert mode(settings.mirror_dir) == 0o700
    assert mode(settings.mirror_dir / snapshot.name) == 0o600


def test_a_restore_writes_owner_only_copies(settings, con, monkeypatch):
    monkeypatch.setattr(restore, "service_answers",
                        lambda settings, timeout=1.0: False)
    con.close()
    snap = mdb.backup(settings)
    os.chmod(snap, 0o644)  # say, one copied back in from elsewhere
    os.umask(0o022)

    result = restore.restore(settings, snap)

    assert mode(result["pre_restore_snapshot"]) == 0o600
    assert mode(settings.db_path) == 0o600
    assert mode(snap) == 0o600
