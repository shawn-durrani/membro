"""Contract tests over the HTTP surface — the suite both repos will run."""

import pytest
from fastapi.testclient import TestClient

from memory_service.api import create_app


@pytest.fixture
def client(settings, fake_llm):
    # Admin-gate v2: GET /v1/facts, GET /v1/review, and the by-id verbs now require
    # the owner admin token even on loopback — carry it as a default header
    # so the rest of this contract suite is unaffected by the tightening.
    app = create_app(settings)
    return TestClient(app, base_url="http://127.0.0.1",
                       headers={"Authorization": f"Bearer {app.state.admin_token}"})


def test_admin_ui_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Held for review" in r.text


def test_health_handshake_shape(client, settings):
    h = client.get("/v1/health").json()
    assert h["status"] == "ok"
    assert h["contract_version"] == settings.contract_version
    for key in ("facts", "messages", "size_bytes", "integrity", "last_backup_at"):
        assert key in h["db"]
    assert "embeddings" in h["capabilities"]


def test_ingest_then_search(client):
    r = client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "c1", "title": "t",
        "messages": [{"external_id": "m1", "speaker": "user",
                       "content": "the kakapo is a flightless parrot",
                       "created_at": "2026-07-01T10:00:00+10:00"}]})
    assert r.json() == {"ingested": 1, "skipped": 0, "attached": 0}
    hits = client.post("/v1/search", json={"query": "kakapo"}).json()["hits"]
    assert len(hits) == 1 and hits[0]["speaker"] == "user"


def test_fact_gate_over_http(client):
    trusted = client.post("/v1/facts", json={
        "content": "Alex cycles to work daily.", "origin_agent": "claude",
        "source_app": "multi-model-chat"}).json()
    assert trusted["quarantined"] is False
    external = client.post("/v1/facts", json={
        "content": "Alex is allergic to shellfish.",
        "origin_agent": "mcp:claude-code"}).json()
    assert external["quarantined"] is True
    review = client.get("/v1/review").json()["facts"]
    assert [f["id"] for f in review] == [external["id"]]


def test_approve_dismiss_delete_flow(client):
    fid = client.post("/v1/facts", json={
        "content": "Alex keeps bees in the backyard.",
        "origin_agent": "mcp:x"}).json()["id"]
    approved = client.post(f"/v1/facts/{fid}/approve").json()
    assert approved["quarantined_at"] is None
    # dismiss requires quarantine — now 409s
    assert client.post(f"/v1/facts/{fid}/dismiss").status_code == 409
    assert client.delete(f"/v1/facts/{fid}").json() == {"deleted": fid}
    assert client.delete(f"/v1/facts/{fid}").status_code == 404


def test_dismiss_all_review_over_http(client):
    # Bulk twin of /facts/{id}/dismiss: clears the whole queue in one
    # call, non-destructively — nothing here bypasses the admin-token gate.
    ids = [client.post("/v1/facts", json={
        "content": f"Alex has a stray fact #{i}.", "origin_agent": "mcp:x"
    }).json()["id"] for i in range(2)]
    assert len(client.get("/v1/review").json()["facts"]) == 2

    r = client.post("/v1/review/dismiss-all")
    assert r.status_code == 200
    assert r.json() == {"dismissed": 2}
    assert client.get("/v1/review").json()["facts"] == []

    # Nothing was deleted — still fetchable, still quarantined, now dismissed.
    quarantined = client.get("/v1/facts", params={"status": "quarantined"}).json()["facts"]
    assert {f["id"] for f in quarantined} == set(ids)
    assert all(f["review_dismissed_at"] for f in quarantined)

    # Idempotent on an empty queue.
    assert client.post("/v1/review/dismiss-all").json() == {"dismissed": 0}


def test_quarantine_accepted_facts_over_http(client):
    # The third treatment for a fact already accepted as canon — neither
    # DELETE (destructive) nor supersede (asserts a replacement that doesn't
    # exist). Batch, with a required reason, skipping rather than failing.
    ids = [client.post("/v1/facts", json={
        "content": f"Alex has a malformed fact #{i}."}).json()["id"]
        for i in range(2)]
    assert client.get("/v1/review").json()["facts"] == []   # all canon, no queue

    r = client.post("/v1/facts/quarantine",
                    json={"ids": ids, "reason": "malformed (early-miner era)"})
    assert r.status_code == 200
    assert r.json() == {"quarantined": ids, "skipped": []}

    # Out of canon, into the queue, reason recorded — and nothing deleted.
    queued = client.get("/v1/review").json()["facts"]
    assert {f["id"] for f in queued} == set(ids)
    assert all(f["quarantine_reason"] == "malformed (early-miner era)" for f in queued)
    assert client.post("/v1/recall", json={"query": "malformed"}).json()["facts"] == []

    # Re-run is a no-op; unknown ids skip rather than 404 the whole batch.
    assert client.post("/v1/facts/quarantine",
                       json={"ids": ids + [999_999], "reason": "again"}
                       ).json() == {"quarantined": [], "skipped": ids + [999_999]}

    # A reason is mandatory — an unexplained row in the queue is unadjudicable.
    assert client.post("/v1/facts/quarantine",
                       json={"ids": ids, "reason": ""}).status_code == 422

    # Reversible via the existing approve.
    assert client.post(f"/v1/facts/{ids[0]}/approve").json()["quarantined_at"] is None


# ── review evidence: the turn a held fact came from ────────────────────────
#
# A reviewer's first question is "who said this", and the queue could not
# answer it. `source` answers it — and answers "nobody knows" honestly, rather
# than pointing at a turn that merely sat nearby.


def _room(con, settings, msgs, conv="room-r"):
    from memory_service import episodic
    episodic.ingest(con, "multi-model-chat", conv, [
        {"external_id": f"m{i}", "speaker": sp, "content": text,
         "created_at": 1700000000.0 + i * 60}
        for i, (sp, text) in enumerate(msgs, start=1)], title="Kitchen chat")
    return conv


def test_review_row_carries_its_source_turn(client, con, settings, fake_llm):
    from memory_service import mining
    _room(con, settings, [
        ("user", "Dinner planning time."),
        ("guest:Sam", "I hate coriander, please leave it out of everything."),
    ])
    fake_llm["response"] = (
        "NEW src=2 importance=4: Alex's wife Sam dislikes coriander.")
    mining.distill(con, settings, "multi-model-chat", "room-r", regenerate=False)

    row = client.get("/v1/review").json()["facts"][0]
    src = row["source"]
    assert src["message_id"] == row["source_message_id"]
    assert src["speaker"] == "guest:Sam" and src["speaker_class"] == "guest"
    assert "coriander" in src["excerpt"] and src["truncated"] is False


def test_review_row_for_an_unbound_fact_names_no_turn(client, con, settings,
                                                      fake_llm):
    from memory_service import mining
    _room(con, settings, [
        ("user", "We are planning this week's meals."),
        ("guest:Sam", "I hate coriander."),
    ], conv="room-u")
    # No src= anywhere, and the retry (fed the same canned answer) supplies
    # none either — the fact is held, unattributed.
    fake_llm["response"] = "NEW importance=4: Alex's wife Sam dislikes coriander."
    mining.distill(con, settings, "multi-model-chat", "room-u", regenerate=False)

    row = client.get("/v1/review").json()["facts"][0]
    assert row["source_message_id"] is None
    assert row["source"] is None        # no turn invented to fill the gap


def test_review_source_excerpt_is_bounded(client, con, settings, fake_llm):
    from memory_service import api, mining
    long_turn = "I hate coriander. " + "We talked about the menu. " * 200
    _room(con, settings, [("user", "Dinner planning time."),
                          ("guest:Sam", long_turn)], conv="room-l")
    fake_llm["response"] = (
        "NEW src=2 importance=4: Alex's wife Sam dislikes coriander.")
    mining.distill(con, settings, "multi-model-chat", "room-l", regenerate=False)

    src = client.get("/v1/review").json()["facts"][0]["source"]
    assert len(src["excerpt"]) == api.REVIEW_EXCERPT_CHARS
    assert src["truncated"] is True     # and it says so, rather than eliding


def test_review_row_without_any_source_message_is_unbound_too(client):
    # Not every held fact comes from mining: an external MCP write has no turn
    # at all. Same honest answer, no crash reaching for a message row.
    client.post("/v1/facts", json={"content": "Alex is allergic to shellfish.",
                                   "origin_agent": "mcp:claude-code"})
    row = client.get("/v1/review").json()["facts"][0]
    assert row["source"] is None


def test_recall_and_summary_endpoints(client, fake_llm):
    client.post("/v1/facts", json={"content": "Alex trains for a triathlon."})
    hits = client.post("/v1/recall", json={"query": "triathlon"}).json()["facts"]
    assert len(hits) == 1 and "embedding" not in hits[0]
    fake_llm["response"] = "## Goals\n- triathlon"
    job = client.post("/v1/summary/regenerate").json()["job_id"]
    import time
    for _ in range(50):
        status = client.get(f"/v1/jobs/{job}").json()
        if status["status"] != "running":
            break
        time.sleep(0.05)
    assert status["status"] == "ok"
    s = client.get("/v1/summary").json()
    assert s["summary"] == "## Goals\n- triathlon" and s["source_fact_ids"]
    # Structured per-fact provenance, additive to contract 1.0 — a
    # client checks THIS for attribution, never the free-form prose.
    assert s["provenance"] == [
        {"id": s["source_fact_ids"][0], "origin_agent": "user",
         "source": "user", "tag": "direct"}]


def test_distill_returns_job(client):
    client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "c9",
        "messages": [{"external_id": "m1", "speaker": "user",
                       "content": "I adopted a greyhound named Biscuit.",
                       "created_at": "2026-07-01T10:00:00+10:00"}]})
    r = client.post("/v1/distill", json={
        "source_app": "multi-model-chat", "conversation_id": "c9"})
    assert r.status_code == 202 and "job_id" in r.json()


def test_error_envelope(client):
    r = client.get("/v1/jobs/nope")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "404", "message": "no such job"}}


def test_bad_date_rejected(client):
    r = client.post("/v1/facts", json={
        "content": "Alex ran a marathon in spring.", "event_date": "not-a-date"})
    assert r.status_code == 422


def test_supersede_endpoint(client):
    a = client.post("/v1/facts", json={"content": "Alex drives a hatchback."}).json()["id"]
    b = client.post("/v1/facts", json={"content": "Alex drives a station wagon."}).json()["id"]
    old = client.post(f"/v1/facts/{a}/supersede", json={"successor_id": b}).json()
    assert old["superseded_by"] == b
    valid = client.get("/v1/facts", params={"status": "valid"}).json()["facts"]
    assert [f["id"] for f in valid] == [b]
