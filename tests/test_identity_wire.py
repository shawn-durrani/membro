"""The wire identity field (#33 slice 4, contract 1.2).

A message on /ingest may now say which person record the sending app
believes spoke, and how it knows. The contract under test:

- the field is stored verbatim beside the message; absent = exactly the
  1.1 behaviour;
- facts born from an identified message link to the person per owner
  decision 3: introduced and owner-correction always bind, voice-match
  binds at confidence 0.8 or above, by-elimination and unknown methods
  never auto-bind;
- a merged-away slug resolves to its winner; forgotten or unknown slugs
  bind nothing;
- the link never changes whether a fact is held - only what review can
  say about it;
- the health handshake announces contract 1.2.
"""

import json

import pytest
from fastapi.testclient import TestClient

from memory_service import api as api_mod
from memory_service import db as mdb
from memory_service import ledger


@pytest.fixture
def client(settings, fake_llm):
    app = api_mod.create_app(settings)
    c = TestClient(app, base_url="http://127.0.0.1",
                   headers={"Authorization": f"Bearer {app.state.admin_token}"})
    c.app = app
    return c


def _person(client, slug="p-sam", name="Sam"):
    r = client.post("/v1/persons", json={
        "slug": slug, "display_name": name,
        "origin_client": "multi-model-chat"})
    assert r.status_code == 200
    return r.json()


def _ingest_with_identity(client, identity, ref="g1"):
    body = {"external_id": ref, "speaker": "guest:sam",
            "content": f"a guest turn worth remembering {ref}",
            "created_at": "2026-08-14T10:00:00+10:00"}
    if identity is not None:
        body["speaker_identity"] = identity
    r = client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "c1",
        "title": "t", "messages": [body]})
    assert r.json()["ingested"] == 1


def _fact_from(client, settings, ref):
    con = mdb.connect(settings.db_path)
    try:
        mid = con.execute("SELECT id FROM messages WHERE external_id=?",
                          (ref,)).fetchone()["id"]
        fid = ledger.add_fact(con, f"Sam said a thing in {ref} today.",
                              client.app.state.settings,
                              origin_agent="miner",
                              source_app="multi-model-chat",
                              conversation_id=1,
                              source_message_id=mid)["id"]
        return con.execute("SELECT person_id, quarantined_at FROM facts "
                           "WHERE id=?", (fid,)).fetchone()
    finally:
        con.close()


def test_identity_is_stored_and_absent_means_1_1(client, settings):
    _ingest_with_identity(client, {"person": "p-sam", "confidence": 0.9,
                                   "method": "voice-match"}, ref="g1")
    _ingest_with_identity(client, None, ref="g2")
    con = mdb.connect(settings.db_path)
    rows = {r["external_id"]: r["speaker_identity"] for r in con.execute(
        "SELECT external_id, speaker_identity FROM messages")}
    con.close()
    assert json.loads(rows["g1"])["method"] == "voice-match"
    assert rows["g2"] == ""


def test_binding_follows_decision_three(client, settings):
    _person(client)
    cases = [
        ({"person": "p-sam", "method": "introduced"}, True),
        ({"person": "p-sam", "method": "owner-correction"}, True),
        ({"person": "p-sam", "method": "voice-match",
          "confidence": 0.8}, True),
        ({"person": "p-sam", "method": "voice-match",
          "confidence": 0.79}, False),
        ({"person": "p-sam", "method": "by-elimination",
          "confidence": 0.99}, False),
        ({"person": "p-sam", "method": "something-new"}, False),
        ({"person": "p-nobody", "method": "introduced"}, False),
        ({"person": None, "method": "introduced"}, False),
    ]
    for i, (identity, should_bind) in enumerate(cases):
        ref = f"g{i}"
        _ingest_with_identity(client, identity, ref=ref)
        row = _fact_from(client, settings, ref)
        assert (row["person_id"] is not None) == should_bind, identity
        # binding never changes holding: trusted-app miner facts stay live
        assert row["quarantined_at"] is None


def test_a_merged_slug_binds_to_the_winner_and_forgotten_binds_nothing(
        client, settings):
    _person(client, slug="p-loser", name="Sammy")
    _person(client, slug="p-winner", name="Sam")
    client.post("/v1/persons/p-loser/merge", json={"into": "p-winner"})
    _ingest_with_identity(client, {"person": "p-loser",
                                   "method": "introduced"}, ref="g1")
    row = _fact_from(client, settings, "g1")
    con = mdb.connect(settings.db_path)
    winner_id = con.execute("SELECT id FROM persons WHERE slug='p-winner'"
                            ).fetchone()["id"]
    con.close()
    assert row["person_id"] == winner_id

    client.post("/v1/persons/p-winner/forget")
    _ingest_with_identity(client, {"person": "p-winner",
                                   "method": "introduced"}, ref="g2")
    assert _fact_from(client, settings, "g2")["person_id"] is None


def test_health_announces_a_contract_that_still_carries_1_3(client):
    # 1.3 (#55) is additive over 1.2, and 1.4 (#84) over 1.3: the version
    # only ever moves up, and a client gating on the major keeps working.
    major, minor = client.get("/v1/health").json()["contract_version"].split(".")
    assert (int(major), int(minor)) >= (1, 3)
