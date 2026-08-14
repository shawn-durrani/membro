"""The message eraser - the human hand behind crossband#106's honesty (#45).

A voice turn discarded at its source may already be ingested here, and the
append-only invariant rightly stops every automated path from touching the
copy. The contract under test:

- resolve: a producer's ref (source_app, conversation, message) maps to our
  row id with an owner-gated preview; unknown refs 404;
- erase: one row by id, owner-gated; the row leaves the archive AND the
  search index (fts stays in sync, health stays ok); live facts mined from
  it quarantine with a source-deleted reason and surface in review; facts
  already held keep their own reason; attachments are counted, never
  cascaded;
- every human eraser (fact / attachment / message) journals a content-free
  row in `erasures`: what was erased is gone, that it was erased is not.
"""

import time

import pytest
from fastapi.testclient import TestClient

from memory_service import db as mdb
from memory_service import ledger
from memory_service.api import create_app


@pytest.fixture
def client(settings, fake_llm):
    app = create_app(settings)
    c = TestClient(app, base_url="http://127.0.0.1",
                   headers={"Authorization": f"Bearer {app.state.admin_token}"})
    c.app = app
    return c


def _ingest(client, ref="m1", content="the kakapo is a flightless parrot"):
    r = client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "c1",
        "title": "birds",
        "messages": [{"external_id": ref, "speaker": "user",
                       "content": content,
                       "created_at": "2026-08-13T10:00:00+10:00"}]})
    assert r.json()["ingested"] == 1


def _row(settings, sql, *args):
    con = mdb.connect(settings.db_path)
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def _msg_id(settings, ref):
    return _row(settings, "SELECT id FROM messages WHERE external_id=?",
                ref)["id"]


def test_resolve_maps_a_producer_ref_to_our_row(client, settings):
    _ingest(client)
    mid = _msg_id(settings, "m1")
    d = client.get("/v1/messages/resolve", params={
        "source_app": "multi-model-chat", "conversation": "c1",
        "message": "m1"}).json()
    assert d["id"] == mid and d["speaker"] == "user"
    assert "kakapo" in d["excerpt"]
    assert d["live_facts"] == 0 and d["attachments"] == 0
    assert client.get("/v1/messages/resolve", params={
        "source_app": "multi-model-chat", "conversation": "c1",
        "message": "nope"}).status_code == 404


def test_erase_removes_the_row_and_search_stays_honest(client, settings):
    _ingest(client, ref="m1", content="the kakapo is a flightless parrot")
    _ingest(client, ref="m2", content="the kea is an alpine parrot")
    mid = _msg_id(settings, "m1")
    r = client.delete(f"/v1/messages/{mid}")
    assert r.json() == {"deleted": mid, "facts_held": 0,
                        "attachments_kept": 0}
    # gone from the archive and from search; the neighbour is untouched
    assert client.post("/v1/search",
                       json={"query": "kakapo"}).json()["hits"] == []
    assert len(client.post("/v1/search",
                           json={"query": "kea"}).json()["hits"]) == 1
    h = client.get("/v1/health").json()
    assert h["status"] == "ok" and h["db"]["fts_in_sync"] is True
    assert client.delete(f"/v1/messages/{mid}").status_code == 404


def test_live_facts_resurface_and_held_ones_keep_their_reason(client,
                                                              settings):
    _ingest(client)
    mid = _msg_id(settings, "m1")
    con = mdb.connect(settings.db_path)
    try:
        conv = con.execute("SELECT conversation_id FROM messages WHERE id=?",
                           (mid,)).fetchone()["conversation_id"]
        live = ledger.add_fact(con, "Alex keeps a kakapo as company.",
                               settings, origin_agent="user",
                               conversation_id=conv,
                               source_message_id=mid)["id"]
        held = ledger.add_fact(con, "A guest said something held here.",
                               settings, origin_agent="miner",
                               conversation_id=conv, source_message_id=mid,
                               quarantine_reason="guest-attribution: stated by guest")["id"]
    finally:
        con.close()

    r = client.delete(f"/v1/messages/{mid}").json()
    assert r["facts_held"] == 1

    rows = client.get("/v1/review").json()["facts"]
    mine = [f for f in rows if f["id"] == live]
    assert mine and mine[0]["reason_class"] == "source-deleted"
    assert mine[0]["source"] is None       # the origin is honestly gone
    kept = _row(settings, "SELECT quarantine_reason FROM facts WHERE id=?",
                held)
    assert kept["quarantine_reason"].startswith("guest-attribution")


def test_attachments_are_counted_never_cascaded(client, settings):
    _ingest(client)
    mid = _msg_id(settings, "m1")
    con = mdb.connect(settings.db_path)
    try:
        conv = con.execute("SELECT conversation_id FROM messages WHERE id=?",
                           (mid,)).fetchone()["conversation_id"]
        con.execute(
            "INSERT INTO attachments(conversation_id, message_external_id, "
            "filename, mime, size, sha256, stored_name, extracted_text, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (conv, "m1", "notes.txt", "text/plain", 5, "ab" * 32, "ab" * 32,
             "some extracted words", time.time()))
        con.commit()
    finally:
        con.close()
    r = client.delete(f"/v1/messages/{mid}").json()
    assert r["attachments_kept"] == 1
    assert _row(settings,
                "SELECT id FROM attachments WHERE message_external_id=?",
                "m1") is not None


def test_every_eraser_journals_content_free(client, settings):
    _ingest(client, content="the secret kakapo whisper phrase")
    mid = _msg_id(settings, "m1")
    fid = client.post("/v1/facts", json={
        "content": "Alex enjoys long walks daily.",
        "origin_agent": "user"}).json()["id"]
    con = mdb.connect(settings.db_path)
    try:
        conv = con.execute("SELECT id FROM conversations").fetchone()["id"]
        aid = con.execute(
            "INSERT INTO attachments(conversation_id, message_external_id, "
            "filename, mime, size, sha256, stored_name, extracted_text, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (conv, "m9", "notes.txt", "text/plain", 5, "cd" * 32, "cd" * 32,
             "attached secret words", time.time())).lastrowid
        con.commit()
    finally:
        con.close()

    assert client.delete(f"/v1/messages/{mid}").status_code == 200
    assert client.delete(f"/v1/facts/{fid}").status_code == 200
    assert client.delete(f"/v1/attachments/{aid}").status_code == 200

    con = mdb.connect(settings.db_path)
    try:
        rows = con.execute("SELECT ts, kind, ref FROM erasures "
                           "ORDER BY id").fetchall()
    finally:
        con.close()
    assert [r["kind"] for r in rows] == ["message", "fact", "attachment"]
    assert all(r["ts"] > 0 for r in rows)
    joined = " ".join(r["ref"] for r in rows)
    # refs point, they never quote: no erased content may survive in them
    for word in ("whisper", "walks", "secret"):
        assert word not in joined
    assert f"message:{mid}" in joined
    assert "conv:multi-model-chat/c1" in joined


def test_both_routes_are_owner_gated(client, settings):
    _ingest(client)
    mid = _msg_id(settings, "m1")
    bare = TestClient(client.app, base_url="http://127.0.0.1")
    assert bare.get("/v1/messages/resolve", params={
        "source_app": "multi-model-chat", "conversation": "c1",
        "message": "m1"}).status_code in (401, 403)
    assert bare.delete(f"/v1/messages/{mid}").status_code in (401, 403)
    assert _row(settings, "SELECT id FROM messages WHERE id=?",
                mid) is not None
