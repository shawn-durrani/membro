"""Restoring a snapshot must not undo the owner's voice erasures (#129).

The restore (#101) replays the erasure journal onto the older copy. Voice
rows used to be skipped, so a restore brought back deleted clips, whose
audio was already gone, and undid forgets. These pin the replay:

- a clip deleted after the snapshot stays deleted, and so does each clip
  of an `unused` bulk delete;
- a clip moved or merged to another person and then deleted is found
  under its old owner, while bytes another clip still uses are kept;
- a person forgotten after the snapshot stays forgotten, with their clips
  and kept-clip manifests gone and their approved facts back in review;
- a clip row whose audio file is missing is named in the report and kept;
- nothing else changes: facts, messages and other people read the same.

Keyless, no service. The synthetic roster: Alex, Sam, Maya.
"""

import os

import pytest

from memory_service import db as mdb
from memory_service import episodic, ledger, persons, restore

APP = "multi-model-chat"


@pytest.fixture
def no_service(monkeypatch):
    monkeypatch.setattr(restore, "service_answers",
                        lambda settings, timeout=1.0: False)


def _person(con, slug):
    return con.execute("SELECT * FROM persons WHERE slug=?",
                       (slug,)).fetchone()


def _mk(con, settings, slug, name):
    persons.upsert(con, settings, slug=slug, display_name=name,
                   origin_client=APP)
    return _person(con, slug)


def _clip(con, settings, slug, data=None):
    """Store one synthetic clip; returns (anchor id, sha256)."""
    data = data or b"RIFF" + os.urandom(400)
    r = persons.add_clip(con, settings, _person(con, slug), data=data,
                         seconds=3.0, source="accumulated", client=APP)
    row = con.execute("SELECT sha256 FROM voice_anchors WHERE id=?",
                      (r["anchor_id"],)).fetchone()
    return r["anchor_id"], row["sha256"]


def _clips(con, slug):
    return sorted(r["sha256"] for r in con.execute(
        "SELECT a.sha256 FROM voice_anchors a JOIN persons p "
        "ON p.id = a.person_id WHERE p.slug=?", (slug,)))


def _snapshot(con, settings):
    con.close()
    snap = mdb.backup(settings)
    assert snap is not None
    return snap, mdb.connect(settings.db_path)


def _restore(settings, snap):
    live = restore.read_journal(settings.db_path)
    result = restore.restore(settings, snap)
    # the restored journal is whole: a second restore has nothing to add
    assert restore.missing_erasures(
        live, restore.read_journal(settings.db_path)) == []
    return result, mdb.connect(settings.db_path)


def _replayed(**kw):
    counts = {"fact": 0, "attachment": 0, "message": 0, "clip": 0,
              "person": 0, "already_absent": 0}
    counts.update(kw)
    return counts


def test_a_deleted_clip_stays_deleted(settings, con, no_service):
    _mk(con, settings, "p-alex", "Alex")
    gone_id, gone = _clip(con, settings, "p-alex")
    _, kept = _clip(con, settings, "p-alex")
    snap, c = _snapshot(con, settings)

    persons.delete_clip(c, settings, _person(c, "p-alex"), gone_id,
                        reason="rotation")
    c.close()

    result, c = _restore(settings, snap)
    try:
        assert result["replayed"] == _replayed(clip=1)
        assert _clips(c, "p-alex") == [kept]
        assert result["clips_missing_file"] == {"count": 0, "clips": []}
        ref = c.execute("SELECT ref FROM erasures").fetchone()["ref"]
        assert ref.startswith(f"clip:{gone[:12]} person:p-alex")
        assert ref.endswith("reason:rotation")
    finally:
        c.close()


def test_an_unused_bulk_delete_stays_deleted(settings, con, no_service):
    _mk(con, settings, "p-maya", "Maya")
    shas = [_clip(con, settings, "p-maya")[1] for _ in range(3)]
    snap, c = _snapshot(con, settings)

    persons.store_manifest(c, _person(c, "p-maya"), client=APP,
                           shas=shas[:1])
    assert persons.delete_unused(
        c, settings, _person(c, "p-maya"))["deleted"] == 2
    c.close()

    result, c = _restore(settings, snap)
    try:
        assert result["to_replay_by_kind"] == {"voice": 2}
        assert result["replayed"] == _replayed(clip=2)
        assert _clips(c, "p-maya") == shas[:1]
        refs = [r["ref"] for r in c.execute("SELECT ref FROM erasures")]
        assert len(refs) == 2 and all(r.endswith("reason:unused")
                                      for r in refs)
        assert result["clips_missing_file"]["count"] == 0
    finally:
        c.close()


def test_a_forgotten_person_stays_forgotten(settings, con, no_service):
    sam = _mk(con, settings, "p-sam", "Sam")
    shas = [_clip(con, settings, "p-sam")[1] for _ in range(2)]
    persons.store_manifest(con, sam, client=APP, shas=shas)
    fid = ledger.add_fact(con, "Sam plays tennis on Tuesdays.", settings,
                          source="user", origin_agent="user")["id"]
    con.execute("UPDATE facts SET person_id=? WHERE id=?", (sam["id"], fid))
    con.commit()
    snap, c = _snapshot(con, settings)

    persons.forget(c, settings, _person(c, "p-sam"))
    live_ts = c.execute("SELECT ts FROM erasures").fetchone()["ts"]
    c.close()

    result, c = _restore(settings, snap)
    try:
        assert result["replayed"] == _replayed(person=1)
        sam = _person(c, "p-sam")
        assert sam["forgotten_at"] == live_ts
        assert _clips(c, "p-sam") == []
        assert c.execute("SELECT COUNT(*) n FROM clip_manifests "
                         "WHERE person_id=?", (sam["id"],)).fetchone()["n"] == 0
        fact = c.execute("SELECT quarantined_at, quarantine_reason "
                         "FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact["quarantined_at"]
        assert fact["quarantine_reason"].startswith("person-forgotten")
        assert result["clips_missing_file"]["count"] == 0
    finally:
        c.close()


def test_a_clip_moved_then_deleted_is_found_under_its_old_owner(
        settings, con, no_service):
    _mk(con, settings, "p-alex", "Alex")
    _mk(con, settings, "p-sam", "Sam")
    moved_id, _ = _clip(con, settings, "p-alex")
    snap, c = _snapshot(con, settings)

    persons.move_clip(c, settings, _person(c, "p-alex"), moved_id,
                      _person(c, "p-sam"))
    persons.delete_clip(c, settings, _person(c, "p-sam"), moved_id)
    c.close()

    result, c = _restore(settings, snap)
    try:
        # the move itself isn't an erasure, so the restored copy files the
        # clip under Alex again; the delete said the bytes went for good
        assert result["replayed"] == _replayed(clip=1)
        assert _clips(c, "p-alex") == [] and _clips(c, "p-sam") == []
        assert result["clips_missing_file"]["count"] == 0
    finally:
        c.close()


def test_a_clip_merged_then_deleted_is_found_under_its_old_owner(
        settings, con, no_service):
    _mk(con, settings, "p-maya", "Maya")
    _mk(con, settings, "p-maya2", "Maya M")
    merged_id, _ = _clip(con, settings, "p-maya2")
    _, kept = _clip(con, settings, "p-maya")
    snap, c = _snapshot(con, settings)

    persons.merge(c, settings, _person(c, "p-maya2"), _person(c, "p-maya"))
    persons.delete_clip(c, settings, _person(c, "p-maya"), merged_id)
    c.close()

    result, c = _restore(settings, snap)
    try:
        assert result["replayed"] == _replayed(clip=1)
        assert _clips(c, "p-maya2") == [] and _clips(c, "p-maya") == [kept]
        assert result["clips_missing_file"]["count"] == 0
    finally:
        c.close()


def test_bytes_another_person_still_uses_are_kept(settings, con, no_service):
    _mk(con, settings, "p-alex", "Alex")
    _mk(con, settings, "p-sam", "Sam")
    shared = b"RIFF" + os.urandom(400)
    alex_id, sha = _clip(con, settings, "p-alex", shared)
    _clip(con, settings, "p-sam", shared)
    snap, c = _snapshot(con, settings)

    persons.delete_clip(c, settings, _person(c, "p-alex"), alex_id)
    c.close()

    result, c = _restore(settings, snap)
    try:
        assert result["replayed"] == _replayed(clip=1)
        assert _clips(c, "p-alex") == [] and _clips(c, "p-sam") == [sha]
        assert result["clips_missing_file"]["count"] == 0
    finally:
        c.close()


def test_a_clip_row_whose_file_is_missing_is_named_and_kept(
        settings, con, no_service):
    _mk(con, settings, "p-maya", "Maya")
    lost_id, lost = _clip(con, settings, "p-maya")
    _clip(con, settings, "p-maya")
    snap, c = _snapshot(con, settings)
    c.close()
    # the audio goes missing outside any erasure, so the journal can't
    # account for it
    (settings.data_dir / "voice_anchors" / f"{lost}.wav").unlink()

    result, c = _restore(settings, snap)
    try:
        assert result["replayed"] == _replayed()
        assert result["clips_missing_file"] == {
            "count": 1, "clips": [{"person": "p-maya", "clip": lost_id}]}
        assert lost in _clips(c, "p-maya")
    finally:
        c.close()


def test_the_voice_replay_leaves_everything_else_alone(
        settings, con, no_service):
    episodic.ingest(con, APP, "chat-1", [
        {"external_id": "m1", "speaker": "user",
         "content": "Alex booked the Fairhaven ferry", "created_at": 1700000000.0},
        {"external_id": "m2", "speaker": "claude",
         "content": "the early one?", "created_at": 1700000060.0},
    ], title="travel")
    _mk(con, settings, "p-alex", "Alex")
    sam = _mk(con, settings, "p-sam", "Sam")
    _mk(con, settings, "p-maya", "Maya")
    alex_gone, _ = _clip(con, settings, "p-alex")
    _clip(con, settings, "p-alex")
    _clip(con, settings, "p-sam")
    _clip(con, settings, "p-maya")
    ledger.add_fact(con, "Alex prefers tea in the afternoon.", settings,
                    source="user", origin_agent="user")
    sams = ledger.add_fact(con, "Sam plays tennis on Tuesdays.", settings,
                           source="user", origin_agent="user")["id"]
    con.execute("UPDATE facts SET person_id=? WHERE id=?", (sam["id"], sams))
    con.commit()

    def state(c):
        return {
            "facts": [dict(r) for r in c.execute(
                "SELECT * FROM facts WHERE id<>? ORDER BY id", (sams,))],
            "messages": [dict(r) for r in c.execute(
                "SELECT * FROM messages ORDER BY id")],
            "maya": dict(_person(c, "p-maya")),
            "maya_clips": _clips(c, "p-maya"),
            "aliases": [dict(r) for r in c.execute(
                "SELECT * FROM person_aliases ORDER BY alias")],
            "alex": {k: v for k, v in dict(_person(c, "p-alex")).items()
                     if k != "updated_at"},
        }

    before = state(con)
    alex_clips = _clips(con, "p-alex")
    snap, c = _snapshot(con, settings)

    persons.delete_clip(c, settings, _person(c, "p-alex"), alex_gone)
    persons.forget(c, settings, _person(c, "p-sam"))
    c.close()

    result, c = _restore(settings, snap)
    try:
        assert result["replayed"] == _replayed(clip=1, person=1)
        assert state(c) == before
        assert len(_clips(c, "p-alex")) == len(alex_clips) - 1
    finally:
        c.close()


def test_a_dry_run_counts_voice_erasures_and_changes_nothing(
        settings, con, no_service):
    _mk(con, settings, "p-sam", "Sam")
    _clip(con, settings, "p-sam")
    snap, c = _snapshot(con, settings)
    persons.forget(c, settings, _person(c, "p-sam"))
    c.close()
    before = settings.db_path.stat().st_mtime_ns

    result = restore.restore(settings, snap, dry_run=True)
    assert result["to_replay"] == 1
    assert result["to_replay_by_kind"] == {"voice": 1}
    assert "replayed" not in result and "clips_missing_file" not in result
    assert settings.db_path.stat().st_mtime_ns == before
