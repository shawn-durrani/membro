"""Guest-present hold (#93, contract 1.5): a fact a model saved directly while
a guest was in the room is held for review. The mined path already holds a
guest's words because each ingested message names its speaker; the direct
save named nobody, so a guest's claim relayed by a model reached canon
unheld. The stamp rides /v1/facts like web_sources does, and the ledger
applies the hold at the same choke point."""

import pytest
from fastapi.testclient import TestClient

from memory_service import db as db_mod
from memory_service import ledger, recall, summary
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


def _save(client, **extra):
    body = {"content": "the fence along the back lane needs a new post",
            "origin_agent": "claude-x", "source_app": "multi-model-chat"}
    body.update(extra)
    r = client.post("/v1/facts", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_explicit_save_with_stamp_is_held_as_guest_present(client, settings):
    res = _save(client, guest_speakers=["guest:Alex"])
    assert res["quarantined"] is True
    reason = _reason(settings, res["id"])
    assert reason == ("guest-present: Alex was in the room when a model saved "
                      "this; held for review before it becomes canon")
    assert reason_class(reason) == "guest-present"


def test_explicit_save_without_stamp_stays_trusted(client):
    assert _save(client)["quarantined"] is False
    assert _save(client, content="an empty stamp is no stamp at all",
                 guest_speakers=[])["quarantined"] is False


def test_unknown_guest_reads_as_unidentified(client, settings):
    res = _save(client, guest_speakers=["guest:Alex", "guest:unknown"])
    reason = _reason(settings, res["id"])
    assert reason.startswith("guest-present: Alex and an unidentified guest were ")


def test_origin_gate_outranks_the_guest_stamp(settings, con):
    res = ledger.add_fact(
        con, "a claim from an app nobody registered", settings,
        source="model", origin_agent="stranger", source_app="unknown-app",
        guest_speakers=["guest:Alex"])
    assert res["quarantined"] is True
    reason = con.execute("SELECT quarantine_reason FROM facts WHERE id=?",
                         (res["id"],)).fetchone()[0]
    assert reason.startswith("external write")  # the stronger gate names it
    assert "guest-present" not in reason


def test_web_stamp_keeps_the_slot_and_the_guest_clause_follows(client, settings):
    res = _save(client, web_sources=["docs.example"], guest_speakers=["guest:Alex"])
    assert res["quarantined"] is True
    reason = _reason(settings, res["id"])
    assert reason.startswith("web-derived: docs.example")
    assert "; guest-present: Alex was in the room" in reason
    assert reason_class(reason) == "web-derived"


def test_the_owner_saving_by_hand_is_still_held_when_stamped(settings, con):
    # The stamp says who was in the room, not who typed. A registered app
    # that stamps an owner-origin save has said a guest was present, and
    # the hold is about what the fact may have come from.
    res = ledger.add_fact(con, "the owner typed this with a guest present",
                          settings, guest_speakers=["guest:Sam"])
    assert res["quarantined"] is True


def test_held_guest_present_fact_never_reaches_recall_or_summary(con, settings, fake_llm):
    ledger.add_fact(con, "Alex enjoys hiking in the mountains.", settings)
    q = ledger.add_fact(con, "Alex secretly lives in Atlantis.", settings,
                        origin_agent="claude-x", source_app="multi-model-chat",
                        guest_speakers=["guest:Sam"])
    assert q["quarantined"] is True
    hits = recall.recall(con, settings, "Atlantis lives")
    assert all(f["id"] != q["id"] for f in hits)
    fake_llm["response"] = "profile text"
    summary.regenerate(con, settings)
    assert q["id"] not in summary.get(con)["source_fact_ids"]


def test_approving_the_held_fact_releases_it(client, settings):
    res = _save(client, guest_speakers=["guest:Alex"])
    fid = res["id"]
    assert client.post(f"/v1/facts/{fid}/approve").status_code == 200
    valid = client.get("/v1/facts", params={"status": "valid"}).json()["facts"]
    assert any(f["id"] == fid for f in valid)
