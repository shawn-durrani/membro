"""Contract 1.8 (#127): a capture app says which voice clips it keeps, and
the owner deletes the rest.

Crossband keeps a few clips per person and drops the others as better ones
arrive. Membro kept every clip it was ever sent. From 1.8:

- `PUT /v1/persons/{slug}/manifest` stores the app's kept-set manifest,
  the sha256 of each clip its bank holds for that person. Storing it
  deletes nothing, however little it lists.
- `GET /v1/persons` counts, per person, the stored clips that app uploaded
  before the manifest arrived and the manifest leaves out (`unused_clips`).
  A clip stored after the manifest is never counted by it, and a manifest
  older than the person's last change counts nothing until a fresh one
  arrives. No manifest, nothing counted.
- `DELETE /v1/persons/{slug}/unused-clips` and `DELETE /v1/unused-clips`
  are the owner's press. They work the set out again, delete the rows and
  the unshared files, and journal one content-free `voice` row per clip,
  marked `reason:unused`.
- A clip delete may say why the app dropped the clip (`reason`: rotation,
  settled, set-aside), journalled on the same row, so the app's drops are
  ordinary deletes. Any other reason is ignored.
- Every route is owner-gated, and a forget drops the person's manifests.

Additive on 1.7, so the older contract tests keep passing beside these.
The synthetic roster: Alex, Sam, Maya.
"""

import base64
import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from memory_service import api as api_mod
from memory_service import db as mdb

APP = "multi-model-chat"


@pytest.fixture
def client(settings, fake_llm):
    app = api_mod.create_app(settings)
    c = TestClient(app, base_url="http://127.0.0.1",
                   headers={"Authorization": f"Bearer {app.state.admin_token}"})
    c.settings = settings
    return c


def _person(client, slug, name):
    r = client.post("/v1/persons", json={"slug": slug, "display_name": name,
                                         "origin_client": APP})
    assert r.status_code == 200, r.text


def _clip(client, slug, client_name=APP):
    """Upload a synthetic clip; returns its sha256."""
    data = b"RIFF" + os.urandom(400)
    r = client.post(f"/v1/persons/{slug}/anchors", json={
        "data_b64": base64.b64encode(data).decode(), "seconds": 3.0,
        "source": "accumulated", "client": client_name})
    assert r.status_code == 200, r.text
    return hashlib.sha256(data).hexdigest()


def _manifest(client, slug, shas, client_name=APP):
    return client.put(f"/v1/persons/{slug}/manifest",
                      json={"client": client_name, "sha256": shas})


def _people(client):
    return {p["slug"]: p for p in client.get("/v1/persons").json()["persons"]}


def _anchors(client, slug):
    return [a["sha256"] for a in
            client.get(f"/v1/persons/{slug}/anchors").json()["anchors"]]


def _files(settings):
    d = settings.data_dir / "voice_anchors"
    return sorted(os.listdir(d)) if d.exists() else []


def _journal(settings):
    c = mdb.connect(settings.db_path)
    try:
        return [(r["kind"], r["ref"]) for r in c.execute(
            "SELECT kind, ref FROM erasures ORDER BY id")]
    finally:
        c.close()


@pytest.fixture
def alex(client):
    """Alex with five stored clips from the app, and no manifest yet."""
    _person(client, "p-alex", "Alex")
    shas = [_clip(client, "p-alex") for _ in range(5)]
    return shas


# ---- handshake ----

def test_health_speaks_1_8(client):
    assert client.get("/v1/health").json()["contract_version"] == "1.8"


# ---- the manifest is stored, and deletes nothing ----

def test_a_manifest_is_stored_and_counts_the_unused_clips(client, alex):
    assert _people(client)["p-alex"]["unused_clips"] == 0   # no manifest yet
    r = _manifest(client, "p-alex", alex[:2])
    assert r.status_code == 200, r.text
    assert r.json() == {"stored": 2, "unused_clips": 3}
    assert _people(client)["p-alex"]["unused_clips"] == 3
    c = mdb.connect(client.settings.db_path)
    try:
        (row,) = c.execute("SELECT client, shas FROM clip_manifests").fetchall()
    finally:
        c.close()
    assert row["client"] == APP
    assert sorted(alex[:2]) == json.loads(row["shas"])
    # a fresh manifest replaces the last one
    assert _manifest(client, "p-alex", alex[:4]).json()["unused_clips"] == 1


def test_nothing_is_deleted_on_the_manifest_alone(client, alex):
    files = _files(client.settings)
    assert _manifest(client, "p-alex", []).json()["unused_clips"] == 5
    assert _anchors(client, "p-alex") == alex
    assert _files(client.settings) == files
    assert _journal(client.settings) == []
    assert _people(client)["p-alex"]["clip_count"] == 5


def test_a_clip_stored_after_the_manifest_is_never_counted(client, alex):
    _manifest(client, "p-alex", alex)
    _clip(client, "p-alex")                     # arrives before the next one
    assert _people(client)["p-alex"]["unused_clips"] == 0


def test_a_manifest_older_than_the_last_change_counts_nothing(client, alex):
    _manifest(client, "p-alex", alex[:2])
    r = client.patch("/v1/persons/p-alex", json={"display_name": "Alexandra"})
    assert r.status_code == 200
    assert _people(client)["p-alex"]["unused_clips"] == 0
    assert client.delete("/v1/persons/p-alex/unused-clips").json()[
        "deleted"] == 0
    assert _anchors(client, "p-alex") == alex
    _manifest(client, "p-alex", alex[:2])      # the app's next pass
    assert _people(client)["p-alex"]["unused_clips"] == 3


def test_only_the_manifests_own_app_is_judged(client, alex):
    other = _clip(client, "p-alex", client_name="another-app")
    _manifest(client, "p-alex", alex)
    assert _people(client)["p-alex"]["unused_clips"] == 0
    assert other in _anchors(client, "p-alex")


def test_a_bad_manifest_is_refused(client, alex):
    assert _manifest(client, "p-alex", ["not-a-sha"]).status_code == 422
    assert _manifest(client, "p-alex", ["a" * 64] * 2,
                     client_name="").status_code == 422
    assert _manifest(client, "p-alex",
                     [f"{i:064x}" for i in range(1001)]).status_code == 422
    assert _manifest(client, "p-nobody", alex).status_code == 404
    assert _people(client)["p-alex"]["unused_clips"] == 0


# ---- the owner's press ----

def test_the_owners_delete_removes_files_and_rows_and_journals(client, alex):
    _person(client, "p-sam", "Sam")
    sam = [_clip(client, "p-sam") for _ in range(2)]
    _manifest(client, "p-alex", alex[:2])
    _manifest(client, "p-sam", sam)
    r = client.delete("/v1/persons/p-alex/unused-clips")
    assert r.status_code == 200, r.text
    assert r.json() == {"slug": "p-alex", "deleted": 3, "files_removed": 3}
    assert _anchors(client, "p-alex") == alex[:2]
    assert sorted(_files(client.settings)) == sorted(
        f"{s}.wav" for s in alex[:2] + sam)
    journal = _journal(client.settings)
    assert len(journal) == 3
    for (kind, ref), sha in zip(journal, alex[2:]):
        assert kind == "voice"
        assert ref == (f"clip:{sha[:12]} person:p-alex file_removed:True "
                       "reason:unused")
    # content-free: no audio, no name, only ids and the reason
    assert "Alex" not in " ".join(ref for _, ref in journal)
    people = _people(client)
    assert people["p-alex"]["unused_clips"] == 0
    assert people["p-sam"]["unused_clips"] == 0
    assert _anchors(client, "p-sam") == sam


def test_one_press_for_everyone(client, alex):
    _person(client, "p-maya", "Maya")
    maya = [_clip(client, "p-maya") for _ in range(3)]
    _manifest(client, "p-alex", alex[:1])
    _manifest(client, "p-maya", maya[:2])
    assert sum(p["unused_clips"] for p in _people(client).values()) == 5
    r = client.delete("/v1/unused-clips")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 5, "files_removed": 5, "persons": 2}
    assert _anchors(client, "p-alex") == alex[:1]
    assert _anchors(client, "p-maya") == maya[:2]
    assert len(_journal(client.settings)) == 5
    again = client.delete("/v1/unused-clips").json()
    assert again == {"deleted": 0, "files_removed": 0, "persons": 0}


def test_bytes_another_person_shares_stay_on_disk(client):
    _person(client, "p-alex", "Alex")
    _person(client, "p-sam", "Sam")
    data = b"RIFF" + os.urandom(400)
    for slug in ("p-alex", "p-sam"):
        assert client.post(f"/v1/persons/{slug}/anchors", json={
            "data_b64": base64.b64encode(data).decode(),
            "client": APP}).status_code == 200
    _manifest(client, "p-alex", [])
    r = client.delete("/v1/persons/p-alex/unused-clips").json()
    assert r["deleted"] == 1 and r["files_removed"] == 0
    assert _files(client.settings) == [f"{hashlib.sha256(data).hexdigest()}.wav"]


# ---- the app's own drops ----

@pytest.mark.parametrize("reason", ["rotation", "settled", "set-aside"])
def test_a_dropped_clip_is_deleted_and_journals_its_reason(client, alex,
                                                          reason):
    aid = client.get("/v1/persons/p-alex/anchors").json()["anchors"][0]["id"]
    r = client.delete(f"/v1/persons/p-alex/anchors/{aid}",
                      params={"reason": reason})
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert alex[0] not in _anchors(client, "p-alex")
    assert f"{alex[0]}.wav" not in _files(client.settings)
    ((kind, ref),) = _journal(client.settings)
    assert kind == "voice" and ref.endswith(f"reason:{reason}")


def test_an_unknown_reason_is_ignored(client, alex):
    aid = client.get("/v1/persons/p-alex/anchors").json()["anchors"][0]["id"]
    r = client.delete(f"/v1/persons/p-alex/anchors/{aid}",
                      params={"reason": "because I said so"})
    assert r.status_code == 200
    ((_, ref),) = _journal(client.settings)
    assert "reason" not in ref


# ---- gates and forget ----

def test_every_new_route_is_owner_gated(client, alex):
    anon = TestClient(client.app, base_url="http://127.0.0.1")
    assert anon.put("/v1/persons/p-alex/manifest",
                    json={"client": APP, "sha256": []}).status_code == 401
    assert anon.delete("/v1/persons/p-alex/unused-clips").status_code == 401
    assert anon.delete("/v1/unused-clips").status_code == 401
    assert _anchors(client, "p-alex") == alex


def test_forget_drops_the_persons_manifests(client, alex):
    _manifest(client, "p-alex", alex[:2])
    assert client.post("/v1/persons/p-alex/forget").status_code == 200
    c = mdb.connect(client.settings.db_path)
    try:
        assert c.execute("SELECT COUNT(*) FROM clip_manifests").fetchone()[0] == 0
    finally:
        c.close()
    assert _manifest(client, "p-alex", alex).status_code == 410
