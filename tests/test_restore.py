"""Restoring a snapshot must not undo the owner's erasures (#101).

The journal lives inside the database, so a plain copy of an older snapshot
brings back every row erased since, tombstones and all. `restore()` reads
the live journal first and replays what the restored copy lacks through the
same erasers the routes use. Keyless; no service runs."""

import time

import pytest

from memory_service import db as mdb
from memory_service import episodic, erasers, ledger, restore


def _fact(con, settings, text):
    return ledger.add_fact(con, text, settings, source="user",
                           origin_agent="user")["id"]


def _msg_id(con, ext):
    return con.execute("SELECT id FROM messages WHERE external_id=?",
                       (ext,)).fetchone()["id"]


@pytest.fixture
def no_service(monkeypatch):
    monkeypatch.setattr(restore, "service_answers", lambda settings, timeout=1.0: False)


def test_restore_replays_erasures_made_after_the_snapshot(settings, con, no_service):
    episodic.ingest(con, "multi-model-chat", "chat-1", [
        {"external_id": "m1", "speaker": "user",
         "content": "the kakapo is a flightless parrot", "created_at": 1700000000.0},
        {"external_id": "m2", "speaker": "claude",
         "content": "a nocturnal one too", "created_at": 1700000060.0},
    ], title="birds")
    old_fact = _fact(con, settings, "Alex keeps a kakapo calendar on the wall.")
    kept_fact = _fact(con, settings, "Alex prefers tea in the afternoon.")
    con.close()
    snap = mdb.backup(settings)
    assert snap is not None

    # after the snapshot: erase an old fact and a message, create and erase a new one
    c = mdb.connect(settings.db_path)
    assert erasers.erase_fact(c, old_fact)
    m1 = _msg_id(c, "m1")
    assert erasers.erase_message(c, m1)["deleted"] == m1
    new_fact = _fact(c, settings, "Alex started fencing lessons.")
    assert erasers.erase_fact(c, new_fact)
    c.close()

    result = restore.restore(settings, snap)
    assert result["to_replay"] == 3
    assert result["replayed"] == {"fact": 1, "attachment": 0, "message": 1,
                                  "already_absent": 1}
    assert result["pre_restore_snapshot"]

    c = mdb.connect(settings.db_path)
    try:
        ids = {r["id"] for r in c.execute("SELECT id FROM facts")}
        assert old_fact not in ids and new_fact not in ids and kept_fact in ids
        assert c.execute("SELECT COUNT(*) n FROM messages WHERE external_id='m1'"
                         ).fetchone()["n"] == 0
        assert c.execute("SELECT COUNT(*) n FROM messages WHERE external_id='m2'"
                         ).fetchone()["n"] == 1
        rows = c.execute("SELECT kind, ref FROM erasures ORDER BY id").fetchall()
        assert [r["kind"] for r in rows] == ["fact", "message", "fact"]
        assert all(v["in_sync"] for v in mdb.fts_status(c).values())
    finally:
        c.close()


def test_dry_run_changes_nothing(settings, con, no_service):
    fid = _fact(con, settings, "Alex keeps a kakapo calendar on the wall.")
    con.close()
    snap = mdb.backup(settings)
    c = mdb.connect(settings.db_path)
    erasers.erase_fact(c, fid)
    c.close()
    before = settings.db_path.stat().st_mtime_ns
    result = restore.restore(settings, snap, dry_run=True)
    assert result["to_replay"] == 1 and result["dry_run"]
    assert settings.db_path.stat().st_mtime_ns == before
    c = mdb.connect(settings.db_path)
    try:
        assert c.execute("SELECT COUNT(*) n FROM facts").fetchone()["n"] == 0
    finally:
        c.close()


def test_refuses_while_the_service_answers(settings, con, monkeypatch):
    con.close()
    snap = mdb.backup(settings)
    monkeypatch.setattr(restore, "service_answers", lambda s, timeout=1.0: True)
    with pytest.raises(RuntimeError, match="stop the service"):
        restore.restore(settings, snap)


def test_refuses_a_file_that_is_not_a_membro_database(settings, con, no_service, tmp_path):
    con.close()
    bogus = tmp_path / "notes.db"
    bogus.write_bytes(b"not a database at all")
    with pytest.raises((ValueError, Exception)):
        restore.restore(settings, bogus)
    with pytest.raises(FileNotFoundError):
        restore.restore(settings, tmp_path / "missing.db")


def test_missing_erasures_compares_by_content_not_id():
    live = [{"id": 5, "ts": 1.0, "kind": "fact", "ref": "fact:1"},
            {"id": 6, "ts": 2.0, "kind": "fact", "ref": "fact:2"}]
    restored = [{"id": 1, "ts": 1.0, "kind": "fact", "ref": "fact:1"}]
    assert restore.missing_erasures(live, restored) == [live[1]]


def test_the_routes_still_erase_through_the_shared_module(settings, fake_llm):
    """The danger-zone routes and the restore must erase the same way, so
    the routes now call erasers.*; a 404 still comes back for a missing row."""
    from fastapi.testclient import TestClient
    from memory_service.api import create_app
    app = create_app(settings)
    auth = {"Authorization": f"Bearer {app.state.admin_token}"}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        fid = client.post("/v1/facts", json={"content": "Alex enjoys long walks daily.",
                                             "origin_agent": "user"}).json()["id"]
        assert client.delete(f"/v1/facts/{fid}").json() == {"deleted": fid}
        assert client.delete(f"/v1/facts/{fid}").status_code == 404
        assert client.delete("/v1/messages/999").status_code == 404
        assert client.delete("/v1/attachments/999").status_code == 404
        c = mdb.connect(settings.db_path)
        try:
            assert c.execute("SELECT kind, ref FROM erasures").fetchone()["ref"] == f"fact:{fid}"
        finally:
            c.close()
