"""Web-derived hold (#55, contract 1.3): a fact born in a round that read the
web is held for review, whichever door it arrives through. The stamp rides
/v1/ingest per message and /v1/facts per save; the ledger applies the hold at
one choke point, so the miner needs no knowledge of the rule."""

import pytest
from fastapi.testclient import TestClient

from memory_service import db as db_mod
from memory_service import episodic, ledger
from memory_service.api import create_app, reason_class


@pytest.fixture
def client(settings, fake_llm):
    app = create_app(settings)
    return TestClient(app, base_url="http://127.0.0.1",
                      headers={"Authorization": f"Bearer {app.state.admin_token}"})


def _ingest(client, messages):
    r = client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "web1",
        "title": "t", "messages": messages})
    assert r.status_code == 200
    return r


def test_ingest_stores_the_stamp_and_absence_stays_1_2(client, settings):
    _ingest(client, [
        {"external_id": "m1", "speaker": "claude-x",
         "content": "the summit is on the ninth of September",
         "created_at": "2026-08-16T10:00:00+10:00",
         "web_sources": ["news.example"]},
        {"external_id": "m2", "speaker": "user",
         "content": "noted, thanks",
         "created_at": "2026-08-16T10:01:00+10:00"},
    ])
    c = db_mod.connect(settings.db_path)
    try:
        rows = {r["external_id"]: r["web_sources"] for r in
                c.execute("SELECT external_id, web_sources FROM messages")}
    finally:
        c.close()
    assert rows["m1"] == '["news.example"]'
    assert rows["m2"] == ""  # absent field = exactly the 1.2 behaviour


def test_fact_bound_to_a_stamped_message_is_held(client, settings):
    _ingest(client, [
        {"external_id": "m1", "speaker": "claude-x",
         "content": "the vendor's new pricing starts next month",
         "created_at": "2026-08-16T10:00:00+10:00",
         "web_sources": ["vendor.example", "News.Example"]},
    ])
    c = db_mod.connect(settings.db_path)
    try:
        msg = c.execute("SELECT id, conversation_id FROM messages").fetchone()
        # The mining path's call shape: trusted app, bound source message.
        res = ledger.add_fact(
            c, "the vendor's pricing changes next month", settings,
            source="model", origin_agent="claude-x",
            source_app="multi-model-chat",
            conversation_id=msg["conversation_id"], source_message_id=msg["id"])
        fact = c.execute("SELECT quarantine_reason FROM facts WHERE id=?",
                         (res["id"],)).fetchone()
    finally:
        c.close()
    assert res["quarantined"] is True
    reason = fact["quarantine_reason"]
    assert reason.startswith("web-derived: ")
    assert "news.example" in reason and "vendor.example" in reason  # lowered
    assert reason_class(reason) == "web-derived"


def test_unstamped_message_keeps_the_trusted_baseline(settings, con):
    episodic.ingest(con, "multi-model-chat", "conv2", [
        {"external_id": "m1", "speaker": "claude-x", "content": "plain turn",
         "created_at": 1755300000.0}])
    msg = con.execute("SELECT id, conversation_id FROM messages").fetchone()
    res = ledger.add_fact(
        con, "a perfectly ordinary remembered fact", settings,
        source="model", origin_agent="claude-x", source_app="multi-model-chat",
        conversation_id=msg["conversation_id"], source_message_id=msg["id"])
    assert res["quarantined"] is False


def test_explicit_save_with_stamp_is_held(client, settings):
    r = client.post("/v1/facts", json={
        "content": "the library's new API launches at version nine",
        "origin_agent": "claude-x", "source_app": "multi-model-chat",
        "web_sources": ["docs.example"]})
    assert r.status_code == 200
    assert r.json()["quarantined"] is True
    c = db_mod.connect(settings.db_path)
    try:
        reason = c.execute("SELECT quarantine_reason FROM facts").fetchone()[0]
    finally:
        c.close()
    assert reason.startswith("web-derived: docs.example")


def test_explicit_save_without_stamp_stays_trusted(client):
    r = client.post("/v1/facts", json={
        "content": "an unstamped save from the trusted app",
        "origin_agent": "claude-x", "source_app": "multi-model-chat"})
    assert r.status_code == 200
    assert r.json()["quarantined"] is False


def test_origin_gate_outranks_the_web_stamp(settings, con):
    res = ledger.add_fact(
        con, "a claim from an app nobody registered", settings,
        source="model", origin_agent="stranger", source_app="unknown-app",
        web_sources=["evil.example"])
    assert res["quarantined"] is True
    reason = con.execute("SELECT quarantine_reason FROM facts WHERE id=?",
                         (res["id"],)).fetchone()[0]
    assert reason.startswith("external write")  # the stronger gate names it
