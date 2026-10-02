"""Approving a held fact lifts the low confidence its hold gave it (#195).

Two holds mark a fact low as they hold it: the origin gate, so an unreviewed
claim never looks sure, and the miner's checks. Approval used to clear the
hold and leave the low mark, so recall kept handing a fact the owner vouched
for to every AI as doubtful. Now approval gives it what it gets when nothing
holds it, which is high. Holds that never lowered it leave it alone, and so
does a confidence the owner set while the fact was held.
"""

import pytest
from fastapi.testclient import TestClient

from memory_service import ledger, mcp_server, mining, recall
from memory_service.api import create_app


def _confidence(con, fact_id):
    return ledger.get_fact(con, fact_id)["confidence"]


def test_an_approved_external_write_comes_back_high(con, settings):
    fid = ledger.add_fact(con, "Alex prefers dark roast coffee.", settings,
                          origin_agent="mcp:claude-code")["id"]
    assert _confidence(con, fid) == "low"
    assert ledger.approve(con, fid)
    assert _confidence(con, fid) == "high"
    hit = recall.recall(con, settings, "")[0]
    assert hit["id"] == fid and hit["confidence"] == "high"


def test_an_approved_mined_fact_comes_back_high(con, settings,
                                                sample_conversation, fake_llm):
    fake_llm["response"] = (
        "NEW importance=3: Alex is based in Shelbyville.\n"   # ungrounded
        "NONE")
    mining.distill(con, settings, "multi-model-chat", "chat-1",
                   regenerate=False)
    held = ledger.review_queue(con)[0]
    assert held["confidence"] == "low"
    assert held["quarantine_reason"].endswith(ledger.MINER_HOLD_SUFFIX)
    assert ledger.approve(con, held["id"])
    assert _confidence(con, held["id"]) == "high"


@pytest.mark.parametrize("sent", ["high", "medium", "low"])
def test_a_hold_that_kept_the_confidence_leaves_it(con, settings, sent):
    """A web stamp holds a trusted save without touching its confidence,
    so approval has nothing to undo."""
    fid = ledger.add_fact(con, "Alex is reading about lathes.", settings,
                          origin_agent="claude", source_app="multi-model-chat",
                          confidence=sent, web_sources=["example.com"])["id"]
    assert _confidence(con, fid) == sent
    ledger.approve(con, fid)
    assert _confidence(con, fid) == sent


def test_the_owners_own_hold_leaves_a_low_fact_low(con, settings):
    fid = ledger.add_fact(con, "Alex might move to Fairhaven.", settings,
                          confidence="low")["id"]
    ledger.quarantine_many(con, [fid], "checking this one")
    ledger.approve(con, fid)
    assert _confidence(con, fid) == "low"


def test_a_confidence_the_owner_set_while_held_is_kept(con, settings):
    fid = ledger.add_fact(con, "Alex prefers dark roast coffee.", settings,
                          origin_agent="mcp:claude-code")["id"]
    ledger.update_fact(con, fid, confidence="medium")
    ledger.approve(con, fid)
    assert _confidence(con, fid) == "medium"


def test_approving_a_live_fact_changes_nothing(con, settings):
    fid = ledger.add_fact(con, "Alex might take up pottery.", settings,
                          confidence="low")["id"]
    assert ledger.approve(con, fid)
    assert _confidence(con, fid) == "low"
    assert not ledger.approve(con, 999_999)


def test_bulk_approve_lifts_it_too(settings):
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1",
                        headers={"Authorization": f"Bearer {app.state.admin_token}"})
    ids = [client.post("/v1/facts", json={
        "content": f"Alex keeps a note number {i}.",
        "origin_agent": "mcp:claude-code"}).json()["id"] for i in range(2)]
    assert client.post("/v1/facts/bulk-approve",
                       json={"ids": ids}).json()["approved"] == 2
    for fid in ids:
        assert client.get("/v1/facts", params={"q": f"#{fid}"}
                          ).json()["facts"][0]["confidence"] == "high"


def test_the_mcp_tool_stops_marking_an_approved_fact_uncertain(
        con, settings, monkeypatch):
    monkeypatch.setattr(mcp_server, "SETTINGS", settings)
    fid = ledger.add_fact(con, "Alex prefers dark roast coffee.", settings,
                          origin_agent="mcp:claude-code",
                          event_date=1767225600.0)["id"]
    ledger.approve(con, fid)
    line = mcp_server.recall_memory("")
    assert "coffee" in line and "?" not in line.split("]")[0]
