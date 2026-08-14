"""Person records: the fleet's identity home (#33, slice 1).

The five owner decisions (2026-08-14) under test, plus the rules the
design states:

- upsert by slug: owner-set names survive client updates, aliases combine,
  an alias belonging to a different person is refused, and a name membro
  has seen as a MODEL speaker label is refused outright (#65 backstop);
- clips are content-addressed (same bytes = no-op), owner-only on disk,
  and every clip is kept (decision 1);
- sync: ?since= returns changed records, forgotten marks included;
- forget runs the numbered steps: audio deleted (journalled,
  content-free), person marked, that person's APPROVED facts move back
  into review as one person-forgotten group (decision 4), held facts
  stay held, and anchor routes answer 410 afterwards;
- guest-fact linking: an existing fact whose source message was spoken
  by guest:<alias> links to the person on upsert;
- every route refuses callers without the owner token.
"""

import base64
import os
import time

import pytest
from fastapi.testclient import TestClient

from memory_service import api as api_mod
from memory_service import db as mdb
from memory_service import ledger

WAV = b"RIFF" + os.urandom(600)
WAV2 = b"RIFF" + os.urandom(600)


@pytest.fixture
def client(settings, fake_llm):
    app = api_mod.create_app(settings)
    c = TestClient(app, base_url="http://127.0.0.1",
                   headers={"Authorization": f"Bearer {app.state.admin_token}"})
    c.app = app
    c.settings = settings
    return c


def _b64(b):
    return base64.b64encode(b).decode()


def _mk(client, slug="p-alex1", name="Alex", **kw):
    body = {"slug": slug, "display_name": name,
            "origin_client": "multi-model-chat", **kw}
    r = client.post("/v1/persons", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_upsert_owner_name_wins_and_aliases_combine(client, settings):
    p = _mk(client, aliases=["Al"])
    assert p["display_name"] == "Alex" and p["clip_count"] == 0
    # owner renames (admin-side flag set directly - the rename route is
    # the admin surface slice; the RULE is what matters here)
    con = mdb.connect(settings.db_path)
    con.execute("UPDATE persons SET display_name='Alexandra', "
                "name_owner_set=1 WHERE slug='p-alex1'")
    con.commit(); con.close()
    p = _mk(client, name="Alex", aliases=["Ali"])
    assert p["display_name"] == "Alexandra"          # owner-set survives
    assert sorted(a["alias"] for a in p["aliases"]) == ["Al", "Alex", "Ali"]


def test_alias_of_another_person_is_refused(client):
    _mk(client, slug="p-a", name="Sam")
    r = client.post("/v1/persons", json={
        "slug": "p-b", "display_name": "Sammy", "aliases": ["Sam"]})
    assert r.status_code == 409
    assert "already belongs" in r.json()["error"]["message"]


def test_a_model_speaker_label_can_never_become_a_person(client):
    client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "c1",
        "title": "t", "messages": [{
            "external_id": "m1", "speaker": "claude",
            "content": "a model turn arriving on the wire",
            "created_at": "2026-08-13T10:00:00+10:00"}]})
    r = client.post("/v1/persons", json={
        "slug": "p-x", "display_name": "Claude",
        "origin_client": "multi-model-chat"})
    assert r.status_code == 409
    assert "AI participant" in r.json()["error"]["message"]


def test_clips_are_content_addressed_and_owner_only(client, settings):
    _mk(client)
    r = client.post("/v1/persons/p-alex1/anchors", json={
        "data_b64": _b64(WAV), "seconds": 2.0, "score": 0.9,
        "source": "introduction", "client": "multi-model-chat"})
    assert r.json()["deduped"] is False
    assert client.post("/v1/persons/p-alex1/anchors", json={
        "data_b64": _b64(WAV)}).json()["deduped"] is True
    rows = client.get("/v1/persons/p-alex1/anchors").json()["anchors"]
    assert len(rows) == 1 and rows[0]["source"] == "introduction"
    f = client.get(f"/v1/persons/p-alex1/anchors/{rows[0]['id']}/file")
    assert f.status_code == 200 and f.content == WAV
    stored = settings.data_dir / "voice_anchors"
    files = list(stored.iterdir())
    assert len(files) == 1
    assert (files[0].stat().st_mode & 0o777) == 0o600
    assert (stored.stat().st_mode & 0o777) == 0o700


def test_sync_since_returns_changes_with_forgotten_marks(client):
    _mk(client)
    t0 = time.time()
    time.sleep(0.02)
    assert client.get("/v1/persons",
                      params={"since": t0}).json()["persons"] == []
    _mk(client, slug="p-b", name="Robin")
    got = client.get("/v1/persons", params={"since": t0}).json()["persons"]
    assert [p["slug"] for p in got] == ["p-b"]
    client.post("/v1/persons/p-alex1/forget")
    got = client.get("/v1/persons", params={"since": t0}).json()["persons"]
    assert {p["slug"]: bool(p["forgotten_at"]) for p in got} == {
        "p-b": False, "p-alex1": True}


def test_forget_runs_the_numbered_steps(client, settings):
    # a guest message, a fact mined from it, and a person with that alias
    client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "c1",
        "title": "t", "messages": [{
            "external_id": "g1", "speaker": "guest:sam",
            "content": "sam mentioned they play tennis on tuesdays",
            "created_at": "2026-08-13T10:00:00+10:00"}]})
    con = mdb.connect(settings.db_path)
    mid = con.execute("SELECT id FROM messages WHERE speaker='guest:sam'"
                      ).fetchone()["id"]
    approved = ledger.add_fact(con, "Sam plays tennis on Tuesdays.",
                               client.app.state.settings,
                               origin_agent="miner",
                               source_app="multi-model-chat",
                               conversation_id=1,
                               source_message_id=mid)["id"]
    held = ledger.add_fact(con, "Sam said something else held.",
                           client.app.state.settings,
                           origin_agent="miner", conversation_id=1,
                           source_message_id=mid,
                           quarantine_reason="guest-attribution: stated by a guest")["id"]
    con.close()

    p = _mk(client, slug="p-sam", name="Sam")
    assert p["facts_linked"] == 2                    # both rode guest:sam
    client.post("/v1/persons/p-sam/anchors", json={"data_b64": _b64(WAV)})
    client.post("/v1/persons/p-sam/anchors", json={"data_b64": _b64(WAV2)})

    r = client.post("/v1/persons/p-sam/forget").json()
    assert r == {"forgotten": "p-sam", "clips_deleted": 2,
                 "files_removed": 2, "facts_held": 1}
    # audio really gone from disk
    assert list((settings.data_dir / "voice_anchors").iterdir()) == []
    # the approved fact is back in review as a person-forgotten group
    rows = client.get("/v1/review").json()["facts"]
    mine = [f for f in rows if f["id"] == approved]
    assert mine and mine[0]["reason_class"] == "person-forgotten"
    # the already-held fact keeps its own reason
    con = mdb.connect(settings.db_path)
    kept = con.execute("SELECT quarantine_reason FROM facts WHERE id=?",
                       (held,)).fetchone()["quarantine_reason"]
    con.close()
    assert kept.startswith("guest-attribution")
    # the erasure journal gained one content-free row
    con = mdb.connect(settings.db_path)
    rows = con.execute("SELECT kind, ref FROM erasures").fetchall()
    con.close()
    assert [r["kind"] for r in rows] == ["voice"]
    assert "p-sam" in rows[0]["ref"] and "tennis" not in rows[0]["ref"]
    # anchor routes answer 410 gone from now on
    assert client.post("/v1/persons/p-sam/anchors", json={
        "data_b64": _b64(WAV)}).status_code == 410
    assert client.get("/v1/persons/p-sam/anchors").status_code == 410


def test_every_route_is_owner_gated(client):
    _mk(client)
    bare = TestClient(client.app, base_url="http://127.0.0.1")
    assert bare.get("/v1/persons").status_code in (401, 403)
    assert bare.post("/v1/persons", json={
        "slug": "p-z", "display_name": "Z Z"}).status_code in (401, 403)
    assert bare.post("/v1/persons/p-alex1/anchors", json={
        "data_b64": _b64(WAV)}).status_code in (401, 403)
    assert bare.get("/v1/persons/p-alex1/anchors").status_code in (401, 403)
    assert bare.post("/v1/persons/p-alex1/forget").status_code in (401, 403)
