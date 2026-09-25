"""Contract 1.7 (#115): a save on POST /facts may name the caller's
conversation, with the same (source_app, conversation_id) pair /ingest and
/recall use. A save made while guests were in the room then binds to that
conversation, like a fact mined from a guest's turn, so approving it no
longer lets it surface in every chat. Additive on 1.6, so the older
contract tests keep passing beside these."""

import pytest
from fastapi.testclient import TestClient

from memory_service import db as db_mod
from memory_service.api import create_app

APP = "multi-model-chat"


@pytest.fixture
def client(settings, fake_llm):
    app = create_app(settings)
    return TestClient(app, base_url="http://127.0.0.1",
                      headers={"Authorization": f"Bearer {app.state.admin_token}"})


def _ingest(client, conv, text="Let's plan the workshop weekend."):
    r = client.post("/v1/ingest", json={
        "source_app": APP, "conversation_id": conv, "title": "Workshop",
        "messages": [{"external_id": f"{conv}-{len(text)}", "speaker": "user",
                      "content": text,
                      "created_at": "2026-09-25T10:00:00+10:00"}]})
    assert r.status_code == 200, r.text


def _save(client, content, **extra):
    body = {"content": content, "origin_agent": "claude-x", "source_app": APP}
    body.update(extra)
    r = client.post("/v1/facts", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _approve(client, fact_id):
    assert client.post(f"/v1/facts/{fact_id}/approve").status_code == 200


def _recalled(client, query, conv=None):
    body = {"query": query, "limit": 20}
    if conv is not None:
        body.update(source_app=APP, conversation_id=conv)
    return {f["id"] for f in client.post("/v1/recall", json=body).json()["facts"]}


def _conversations(settings, conv):
    c = db_mod.connect(settings.db_path)
    try:
        return [dict(r) for r in c.execute(
            "SELECT id, title FROM conversations WHERE source_app=? "
            "AND external_id=?", (APP, conv))]
    finally:
        c.close()


def _fact_conversation(settings, fact_id):
    c = db_mod.connect(settings.db_path)
    try:
        return c.execute("SELECT conversation_id FROM facts WHERE id=?",
                         (fact_id,)).fetchone()[0]
    finally:
        c.close()


# ---- handshake ----

def test_health_speaks_1_7(client):
    assert client.get("/v1/health").json()["contract_version"] == "1.7"


# ---- a guest-present save that names its conversation ----

def test_a_guest_present_save_binds_to_the_conversation_it_names(client, settings):
    _ingest(client, "room-1")
    _ingest(client, "room-2", "A quiet evening, just Alex.")
    f = _save(client, "Sam's birthday is on the ninth of March.",
              guest_speakers=["guest:Sam"], conversation_id="room-1")
    assert f["quarantined"] is True and f["scope"] == "conversation"

    (row,) = [r for r in client.get("/v1/review").json()["facts"]
              if r["id"] == f["id"]]
    assert row["reason_class"] == "guest-present"
    assert row["conversation"]["external_id"] == "room-1"

    # approving keeps the binding: recalled in its own chat and nowhere else
    _approve(client, f["id"])
    assert f["id"] in _recalled(client, "birthday March", conv="room-1")
    assert f["id"] not in _recalled(client, "birthday March", conv="room-2")
    assert f["id"] not in _recalled(client, "birthday March")


def test_without_the_pair_a_guest_present_save_stays_global(client):
    f = _save(client, "Sam's birthday is on the ninth of April.",
              guest_speakers=["guest:Sam"])
    assert f["quarantined"] is True and f["scope"] == "global"
    _approve(client, f["id"])
    assert f["id"] in _recalled(client, "birthday April")


def test_half_a_pair_names_no_conversation(client, settings):
    _ingest(client, "room-h")
    f = _save(client, "Sam's birthday is on the ninth of May.",
              guest_speakers=["guest:Sam"], source_app=None,
              conversation_id="room-h")
    assert f["scope"] == "global"


def test_a_save_before_the_first_ingest_creates_the_conversation(client, settings):
    """A client hands a chat over when it goes quiet, so a save made in a
    live room can name a chat this service has not seen. The save creates
    the record, and the later ingest fills that same record."""
    assert _conversations(settings, "room-new") == []
    f = _save(client, "Sam's birthday is on the ninth of June.",
              guest_speakers=["guest:unknown"], conversation_id="room-new")
    assert f["quarantined"] is True and f["scope"] == "conversation"
    (conv,) = _conversations(settings, "room-new")
    assert _fact_conversation(settings, f["id"]) == conv["id"]

    _ingest(client, "room-new")
    assert _conversations(settings, "room-new") == [{"id": conv["id"],
                                                     "title": "Workshop"}]
    _approve(client, f["id"])
    assert f["id"] in _recalled(client, "birthday June", conv="room-new")
    assert f["id"] not in _recalled(client, "birthday June")


def test_a_refused_save_leaves_no_conversation_behind(client, settings):
    r = client.post("/v1/facts", json={
        "content": "tiny", "origin_agent": "claude-x", "source_app": APP,
        "guest_speakers": ["guest:Sam"], "conversation_id": "room-refused"})
    assert r.status_code == 422
    assert _conversations(settings, "room-refused") == []


def test_a_stamp_with_no_guest_in_it_creates_nothing(client, settings):
    f = _save(client, "The bandsaw fence needs squaring again.",
              guest_speakers=["claude-x", "user"], conversation_id="room-none")
    assert f["scope"] == "global"
    assert _conversations(settings, "room-none") == []


# ---- the owner's own saves ----

def test_the_owners_save_stays_global_with_a_conversation_named(client, settings):
    _ingest(client, "room-o")
    f = _save(client, "Alex sharpens the chisels every Sunday.",
              conversation_id="room-o")
    assert f["quarantined"] is False and f["scope"] == "global"
    assert f["id"] in _recalled(client, "chisels Sunday")
    assert f["id"] in _recalled(client, "chisels Sunday", conv="room-o")
    # it records where it was made, as a mined fact of the owner's does
    (conv,) = _conversations(settings, "room-o")
    assert _fact_conversation(settings, f["id"]) == conv["id"]


def test_the_owners_save_naming_an_unknown_conversation_creates_nothing(
        client, settings):
    f = _save(client, "Alex oils the workbench top each autumn.",
              conversation_id="room-unseen")
    assert f["scope"] == "global"
    assert _fact_conversation(settings, f["id"]) is None
    assert _conversations(settings, "room-unseen") == []


def test_an_overlong_conversation_id_is_refused_like_recall(client):
    r = client.post("/v1/facts", json={
        "content": "Sam's birthday is on the ninth of July.",
        "origin_agent": "claude-x", "source_app": APP,
        "guest_speakers": ["guest:Sam"], "conversation_id": "x" * 65})
    assert r.status_code == 422
