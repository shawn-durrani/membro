"""GET /v1/busy (#95, workbench#69): the in-flight work a restart would
interrupt, for the fleet's deploy watcher. Open on loopback like /health;
fixed labels, never content; a mark past the ceiling stops counting."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from memory_service import busy, db as db_mod, embeddings, episodic, jobs, judge
from memory_service.api import create_app

IDLE = {"busy": False, "reasons": []}
REGISTRY_KINDS = ["distill", "summary", "consolidate", "viz-embeddings"]


def _client(settings, token=True):
    app = create_app(settings)
    headers = {"Authorization": f"Bearer {app.state.admin_token}"} if token else {}
    return TestClient(app, base_url="http://127.0.0.1", headers=headers)


def _wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _blocking_job(kind):
    """A registry job that runs until `release` is set, the way a distill
    runs until its last chunk is mined."""
    release = threading.Event()
    return jobs.run(kind, release.wait), release


def _done(*job_ids):
    return all(jobs.get(j)["status"] != "running" for j in job_ids)


@pytest.fixture(autouse=True)
def _settled():
    # Another module's fire-and-forget job (a contract test's distill) may
    # still be draining when this module starts. Every test here begins
    # and ends with nothing marked.
    assert _wait_for(lambda: not busy.snapshot()["busy"])
    yield
    assert _wait_for(lambda: not busy.snapshot()["busy"])


# ---- the route ----

def test_fresh_service_is_not_busy(settings, fake_llm):
    r = _client(settings).get("/v1/busy")
    assert r.status_code == 200
    assert r.json() == IDLE


def test_busy_is_open_without_the_owner_credential(settings, fake_llm):
    # The watcher holds no session and no bearer. The route is open the way
    # /v1/health is; its answer carries nothing a gate would protect.
    r = _client(settings, token=False).get("/v1/busy")
    assert r.status_code == 200
    assert r.json() == IDLE


def test_a_202_already_reads_as_busy_over_http(settings, fake_llm, monkeypatch):
    # The mark is placed before the job's thread starts, so there is no
    # window between the 202 and a true answer for the watcher to fall
    # through. The model call blocks until the test lets it go.
    client = _client(settings)
    client.post("/v1/facts", json={"content": "Alex keeps bees in the backyard."})
    release = threading.Event()

    def _slow(prompt, settings, **kw):
        release.wait(5)
        return "## Interests\n- bees"
    monkeypatch.setattr("memory_service.llm.utility_complete", _slow)
    job = client.post("/v1/summary/regenerate").json()["job_id"]
    try:
        assert client.get("/v1/busy").json() == {"busy": True, "reasons": ["summary"]}
    finally:
        release.set()
    assert _wait_for(lambda: _done(job))
    assert jobs.get(job)["status"] == "ok"
    assert client.get("/v1/busy").json() == IDLE


# ---- the registry: every job kind is a label ----

@pytest.mark.parametrize("kind", REGISTRY_KINDS)
def test_a_running_job_holds_its_kind_as_the_reason(kind):
    assert kind in busy.LABELS
    job_id, release = _blocking_job(kind)
    try:
        assert busy.snapshot() == {"busy": True, "reasons": [kind]}
    finally:
        release.set()
    assert _wait_for(lambda: _done(job_id))
    assert busy.snapshot() == IDLE


def test_reasons_are_deduplicated_and_sorted():
    ids, releases = zip(*[_blocking_job(k) for k in ("summary", "distill", "distill")])
    try:
        assert busy.snapshot()["reasons"] == ["distill", "summary"]
    finally:
        for r in releases:
            r.set()
    assert _wait_for(lambda: _done(*ids))


def test_a_failed_job_releases_its_mark():
    def _boom():
        raise RuntimeError("no key configured")
    job_id = jobs.run("consolidate", _boom)
    assert _wait_for(lambda: _done(job_id))
    assert jobs.get(job_id)["status"] == "failed"
    assert busy.snapshot() == IDLE


def test_an_undeclared_label_is_refused():
    # The answer can never carry an id or content: a label is declared in
    # busy.LABELS or it does not exist, and a job kind is a label.
    with pytest.raises(ValueError):
        with busy.mark("distill:chat-123"):
            pass
    with pytest.raises(ValueError):
        jobs.run("distill:chat-123", lambda: None)
    assert busy.snapshot() == IDLE


# ---- the ceiling ----

def test_a_mark_past_the_ceiling_stops_counting():
    with busy.mark("judge"):
        started = time.monotonic()
        assert busy.snapshot(now=started)["busy"]
        assert busy.snapshot(now=started + busy.STALE_AFTER_S - 1)["busy"]
        assert busy.snapshot(now=started + busy.STALE_AFTER_S + 1) == IDLE


def test_a_job_stuck_past_the_ceiling_no_longer_holds_a_deploy():
    # A hung provider call would otherwise hold every deploy for ever. The
    # job row is untouched; only the probe stops counting it.
    job_id, release = _blocking_job("distill")
    try:
        assert busy.snapshot()["busy"]
        assert busy.snapshot(now=time.monotonic() + busy.STALE_AFTER_S + 1) == IDLE
        assert jobs.get(job_id)["status"] == "running"
    finally:
        release.set()
    assert _wait_for(lambda: _done(job_id))


# ---- the marks placed by hand ----

def test_a_backup_reports_busy_while_it_copies(settings, con, monkeypatch):
    seen = []
    real_rotate = db_mod._rotate

    def _spy(folder, keep):
        seen.append(busy.snapshot())
        real_rotate(folder, keep)
    monkeypatch.setattr(db_mod, "_rotate", _spy)
    assert db_mod.backup(settings) is not None
    assert seen == [{"busy": True, "reasons": ["backup"]}]
    assert busy.snapshot() == IDLE


def _held_grounding_row(con):
    episodic.ingest(con, "multi-model-chat", "judge-chat", [
        {"external_id": "m1", "speaker": "user",
         "content": "I toured the zephyrline factory today",
         "created_at": 1700000000.0},
        {"external_id": "m2", "speaker": "user", "content": "nice",
         "created_at": 1700000001.0}], title="t")
    conv = episodic.get_conversation(con, "multi-model-chat", "judge-chat")
    mid = con.execute("SELECT id FROM messages WHERE conversation_id=? ORDER BY id",
                      (conv["id"],)).fetchone()["id"]
    con.execute(
        "INSERT INTO facts(content, source, origin_agent, conversation_id,"
        " source_message_id, created_at, event_date, confidence,"
        " content_hash, quarantined_at, quarantine_reason)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("Alex toured the Zephyrline factory.", "chat", "multi-model-chat",
         conv["id"], mid, 1700000100.0, 1700000100.0, "medium", "h-busy",
         1700000100.0, "grounding: Zephyrline not in source chat"))
    con.commit()


def test_the_judge_pass_reports_busy_while_it_asks_the_model(
        con, settings, fake_llm, monkeypatch):
    settings.judge_pass = True
    _held_grounding_row(con)
    seen = []

    def _spy(prompt, settings, **kw):
        seen.append(busy.snapshot())
        return '{"Zephyrline": "toured the zephyrline factory"}'
    monkeypatch.setattr("memory_service.llm.utility_complete", _spy)
    assert judge.run_pass(con, settings)["examined"] == 1
    assert seen == [{"busy": True, "reasons": ["judge"]}]
    assert busy.snapshot() == IDLE


def test_the_reembed_refill_reports_busy_while_it_runs(con, settings, monkeypatch):
    # The refill starts when the stored embedding space differs from the
    # configured model. The refill itself is stubbed, as test_embedding_space
    # does, and records what the probe said while it ran.
    embeddings.sync_space(con, settings)          # adopt the current model
    seen = []
    monkeypatch.setattr(embeddings, "ensure_fact_embeddings",
                        lambda *a, **k: seen.append(busy.snapshot()))
    changed = settings.model_copy(update={"embedding_model": "another-model"})
    t = embeddings.start_reembed_if_needed(changed)
    assert t is not None
    t.join(timeout=5)
    assert seen == [{"busy": True, "reasons": ["reembed"]}]
    assert busy.snapshot() == IDLE
