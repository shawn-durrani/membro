"""Contract 1.5 (#93): `guest_speakers` on POST /facts. Additive on 1.4, so
the older assertions in test_contract_1_4.py keep passing beside these."""

import pytest
from fastapi.testclient import TestClient

from memory_service import db as db_mod
from memory_service import ledger
from memory_service.api import create_app, reason_class


@pytest.fixture
def client(settings, fake_llm):
    app = create_app(settings)
    return TestClient(app, base_url="http://127.0.0.1",
                      headers={"Authorization": f"Bearer {app.state.admin_token}"})


def _reason(settings, fact_id):
    c = db_mod.connect(settings.db_path)
    try:
        return c.execute("SELECT quarantine_reason FROM facts WHERE id=?",
                         (fact_id,)).fetchone()[0]
    finally:
        c.close()


def _post(client, **extra):
    body = {"content": "the workshop's bandsaw blade wants replacing",
            "origin_agent": "claude-x", "source_app": "multi-model-chat"}
    body.update(extra)
    return client.post("/v1/facts", json=body)


# ---- handshake ----

def test_health_speaks_1_5(client):
    assert client.get("/v1/health").json()["contract_version"] == "1.5"


# ---- the field ----

def test_stamped_save_is_accepted_and_held(client, settings):
    r = _post(client, guest_speakers=["guest:Alex"])
    assert r.status_code == 200, r.text
    assert r.json()["quarantined"] is True
    assert reason_class(_reason(settings, r.json()["id"])) == "guest-present"


def test_body_without_the_field_is_the_1_4_behaviour(client):
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": r.json()["id"], "quarantined": False}


def test_unknown_shapes_are_dropped_not_rejected(client):
    # A model slug, the owner, an empty string, and a class this build does
    # not know: none of them is a guest, so none of them holds the save.
    r = _post(client, guest_speakers=["claude-x", "user", "  ", "visitor:Alex"])
    assert r.status_code == 200, r.text
    assert r.json()["quarantined"] is False


def test_unknown_shapes_beside_a_real_guest_still_hold(client, settings):
    r = _post(client, guest_speakers=["visitor:Sam", "guest:Alex"])
    assert r.json()["quarantined"] is True
    reason = _reason(settings, r.json()["id"])
    assert "Alex" in reason and "Sam" not in reason


def test_entries_are_stripped_and_deduped_in_order(client, settings):
    r = _post(client, guest_speakers=[" guest:Sam ", "guest:Alex", "guest:Sam", ""])
    reason = _reason(settings, r.json()["id"])
    assert reason.startswith("guest-present: Sam and Alex were ")


def test_cap_is_twelve_entries(client):
    twelve = [f"guest:Guest{i}" for i in range(12)]
    assert _post(client, guest_speakers=twelve).status_code == 200
    assert _post(client, guest_speakers=twelve + ["guest:Extra"]).status_code == 422


def test_reason_names_at_most_five_guests(settings, con):
    res = ledger.add_fact(con, "a save made in a crowded room", settings,
                          origin_agent="claude-x", source_app="multi-model-chat",
                          guest_speakers=[f"guest:Guest{i}" for i in range(8)])
    reason = con.execute("SELECT quarantine_reason FROM facts WHERE id=?",
                         (res["id"],)).fetchone()[0]
    assert "Guest4" in reason and "Guest5" not in reason


def test_reason_is_capped_at_300_characters(settings, con):
    long_name = "guest:" + "A" * 400
    res = ledger.add_fact(con, "a save with an absurdly long guest name", settings,
                          origin_agent="claude-x", source_app="multi-model-chat",
                          guest_speakers=[long_name])
    reason = con.execute("SELECT quarantine_reason FROM facts WHERE id=?",
                         (res["id"],)).fetchone()[0]
    assert reason.startswith("guest-present: " + "A" * 300 + " was in the room")
    assert "A" * 301 not in reason


def test_review_row_carries_the_new_class(client):
    r = _post(client, guest_speakers=["guest:unknown"])
    rows = client.get("/v1/review").json()["facts"]
    row = next(x for x in rows if x["id"] == r.json()["id"])
    assert row["reason_class"] == "guest-present"
    assert row["quarantine_reason"].startswith(
        "guest-present: an unidentified guest was in the room")
