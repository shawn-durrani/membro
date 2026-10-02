"""The profile drops a pulled fact on its own (#189).

The owner's decision of 2 October: when a fact is erased or a person is
forgotten, the profile rebuilds straight away. Holds fold into one rebuild
five minutes after the last of them. A rebuild only spends a model call when
the live profile cites a fact that's now held or gone, two builds never run
side by side, and a restart doesn't lose a rebuild that was waiting.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from memory_service import db as mdb
from memory_service import jobs, ledger, summary
from memory_service.api import create_app

PROFILE = "## Identity\n- Alex is a data engineer."


@pytest.fixture
def client(settings, fake_llm):
    app = create_app(settings)
    c = TestClient(app, base_url="http://127.0.0.1",
                   headers={"Authorization": f"Bearer {app.state.admin_token}"})
    c.app = app
    return c


def _facts(con, settings, n=3):
    texts = ["Alex works at Initech as a data engineer.",
             "Alex lives in Fairhaven with Sam.",
             "Alex is training for a half marathon in May.",
             "Alex prefers tea to coffee in the morning."]
    return [ledger.add_fact(con, t, settings)["id"] for t in texts[:n]]


def _build(con, settings, fake_llm):
    fake_llm["response"] = PROFILE
    summary.regenerate(con, settings)
    return summary.get(con)["source_fact_ids"]


def _drafts(fake_llm):
    return sum(1 for r in fake_llm["requests"] if r["site"] == "summary.draft")


def _done(job_id, timeout=10):
    """The job's final record, once its thread has written it."""
    deadline = time.monotonic() + timeout
    while jobs.get(job_id)["status"] == "running":
        assert time.monotonic() < deadline, "the job never finished"
        time.sleep(0.01)
    return jobs.get(job_id)


def _settle(refresh, timeout=10):
    """Wait for a waiting hold rebuild to start, then for it to finish."""
    deadline = time.monotonic() + timeout
    while refresh.waiting():
        assert time.monotonic() < deadline, "the hold rebuild never started"
        time.sleep(0.01)
    assert refresh.wait(timeout)


# ---- the check ----

def test_held_in_profile_names_held_and_erased_ids(con, settings, fake_llm):
    a, b, c = _facts(con, settings)
    assert summary.held_in_profile(con) == []          # no profile yet
    assert sorted(_build(con, settings, fake_llm)) == [a, b, c]
    assert summary.held_in_profile(con) == []
    ledger.quarantine_many(con, [a], "test hold")
    con.execute("DELETE FROM facts WHERE id=?", (c,))
    con.commit()
    assert summary.held_in_profile(con) == [a, c]


def test_drop_held_spends_nothing_when_the_profile_is_clear(con, settings,
                                                            fake_llm):
    a, b, _ = _facts(con, settings)
    _build(con, settings, fake_llm)
    ledger.add_fact(con, "Alex once mentioned a held thing.", settings,
                    quarantine_reason="test hold")   # never in the profile
    calls = len(fake_llm["requests"])
    assert summary.drop_held(con, settings) == {"rebuilt": False,
                                                "held_in_profile": 0}
    assert len(fake_llm["requests"]) == calls


def test_drop_held_rebuilds_without_the_held_fact(con, settings, fake_llm):
    a, b, c = _facts(con, settings)
    _build(con, settings, fake_llm)
    ledger.quarantine_many(con, [b], "test hold")
    out = summary.drop_held(con, settings)
    assert out["rebuilt"] and out["held_in_profile"] == 1
    assert sorted(summary.get(con)["source_fact_ids"]) == [a, c]
    assert summary.held_in_profile(con) == []


# ---- the triggers ----

def test_erasing_a_fact_rebuilds_the_profile_straight_away(client, settings,
                                                           fake_llm):
    con = mdb.connect(settings.db_path)
    a, b, c = _facts(con, settings)
    _build(con, settings, fake_llm)
    assert client.delete(f"/v1/facts/{b}").status_code == 200
    assert client.app.state.profile_refresh.wait(10)
    assert sorted(summary.get(con)["source_fact_ids"]) == [a, c]
    assert _drafts(fake_llm) == 2
    con.close()


def test_erasing_a_fact_the_profile_never_used_costs_nothing(client, settings,
                                                             fake_llm):
    con = mdb.connect(settings.db_path)
    _facts(con, settings)
    _build(con, settings, fake_llm)
    held = ledger.add_fact(con, "Alex once mentioned a held thing.", settings,
                           quarantine_reason="test hold")["id"]
    assert client.delete(f"/v1/facts/{held}").status_code == 200
    assert client.app.state.profile_refresh.wait(10)
    assert _drafts(fake_llm) == 1
    con.close()


def test_erasing_a_message_rebuilds_without_the_facts_it_held(client, settings,
                                                              fake_llm):
    client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "c1",
        "title": "t", "messages": [{
            "external_id": "m1", "speaker": "user",
            "content": "I started at Initech as a data engineer.",
            "created_at": "2026-08-13T10:00:00+10:00"}]})
    con = mdb.connect(settings.db_path)
    mid = con.execute("SELECT id FROM messages").fetchone()["id"]
    bound = ledger.add_fact(con, "Alex works at Initech as a data engineer.",
                            settings, conversation_id=1,
                            source_message_id=mid)["id"]
    other = ledger.add_fact(con, "Alex lives in Fairhaven with Sam.",
                            settings)["id"]
    _build(con, settings, fake_llm)
    r = client.delete(f"/v1/messages/{mid}").json()
    assert r["facts_held"] == 1
    assert client.app.state.profile_refresh.wait(10)
    assert summary.get(con)["source_fact_ids"] == [other]
    assert bound not in summary.get(con)["source_fact_ids"]
    con.close()


def test_forgetting_a_person_rebuilds_without_their_facts(client, settings,
                                                          fake_llm):
    con = mdb.connect(settings.db_path)
    keep, theirs = _facts(con, settings, n=2)
    _build(con, settings, fake_llm)
    assert client.post("/v1/persons", json={
        "slug": "p-sam", "display_name": "Sam",
        "origin_client": "multi-model-chat"}).status_code == 200
    pid = con.execute("SELECT id FROM persons WHERE slug='p-sam'"
                      ).fetchone()["id"]
    con.execute("UPDATE facts SET person_id=? WHERE id=?", (pid, theirs))
    con.commit()
    assert client.post("/v1/persons/p-sam/forget").json()["facts_held"] == 1
    assert client.app.state.profile_refresh.wait(10)
    assert summary.get(con)["source_fact_ids"] == [keep]
    con.close()


def test_a_burst_of_holds_costs_one_rebuild_after_the_last(client, settings,
                                                           fake_llm):
    refresh = client.app.state.profile_refresh
    assert refresh.settle_s == summary.HOLD_SETTLE_S == 300
    refresh.settle_s = 1.0
    con = mdb.connect(settings.db_path)
    ids = _facts(con, settings, n=4)
    _build(con, settings, fake_llm)
    for fid in ids[:3]:
        r = client.post("/v1/facts/quarantine",
                        json={"ids": [fid], "reason": "test hold"})
        assert r.json()["quarantined"] == [fid]
        assert refresh.waiting()          # the hold waits; it doesn't build
        assert _drafts(fake_llm) == 1
        time.sleep(0.1)                   # each hold moves the wait on
    _settle(refresh)
    assert _drafts(fake_llm) == 2         # three holds, one rebuild
    assert summary.get(con)["source_fact_ids"] == [ids[3]]
    con.close()


def test_a_hold_that_skips_everything_waits_for_nothing(client, settings,
                                                        fake_llm):
    con = mdb.connect(settings.db_path)
    a, = _facts(con, settings, n=1)
    ledger.quarantine_many(con, [a], "already held")
    r = client.post("/v1/facts/quarantine", json={"ids": [a], "reason": "x"})
    assert r.json() == {"quarantined": [], "skipped": [a]}
    assert not client.app.state.profile_refresh.waiting()
    con.close()


def test_an_erase_takes_a_waiting_hold_rebuild_with_it(client, settings,
                                                       fake_llm):
    refresh = client.app.state.profile_refresh
    con = mdb.connect(settings.db_path)
    a, b, c = _facts(con, settings)
    _build(con, settings, fake_llm)
    client.post("/v1/facts/quarantine", json={"ids": [a], "reason": "hold"})
    assert refresh.waiting()
    client.delete(f"/v1/facts/{b}")
    assert not refresh.waiting()
    assert refresh.wait(10)
    assert _drafts(fake_llm) == 2
    assert summary.get(con)["source_fact_ids"] == [c]
    con.close()


# ---- never doubled ----

class _Gate:
    """A fake model whose next profile draft blocks until released."""

    def __init__(self, fake_llm, monkeypatch):
        import memory_service.llm as llm
        self.entered = threading.Event()
        self.release = threading.Event()
        self.drafts = 0
        inner = llm.utility_complete

        def gated(prompt, settings, max_tokens=1000, model=None, **kw):
            if kw.get("site") == "summary.draft":
                self.drafts += 1
                if self.drafts == 2:      # the first build after setup
                    self.entered.set()
                    assert self.release.wait(10)
            return inner(prompt, settings, max_tokens=max_tokens,
                         model=model, **kw)

        monkeypatch.setattr("memory_service.llm.utility_complete", gated)


def test_requests_while_a_refresh_runs_fold_into_one_more_pass(
        client, settings, fake_llm, monkeypatch):
    gate = _Gate(fake_llm, monkeypatch)
    refresh = client.app.state.profile_refresh
    con = mdb.connect(settings.db_path)
    a, b, c = _facts(con, settings)
    _build(con, settings, fake_llm)
    ledger.quarantine_many(con, [a], "hold")
    first = refresh.now()
    assert gate.entered.wait(10)          # chose its facts: b and c
    ledger.quarantine_many(con, [b], "hold")
    assert refresh.now() == first         # folded, not a second job
    assert refresh.now() == first
    gate.release.set()
    assert refresh.wait(10)
    assert _done(first)["status"] == "ok"
    assert gate.drafts == 3               # setup, the first pass, one more
    assert summary.get(con)["source_fact_ids"] == [c]
    con.close()


def test_a_refresh_waits_for_a_build_already_running(client, settings,
                                                     fake_llm, monkeypatch):
    """A rebuild started from the admin page chose its facts before the
    erase. The refresh waits for it, sees its profile still cites the
    erased fact, and rebuilds once more. Without the build lock, the
    refresh would finish first and the slower build would put the fact
    back."""
    gate = _Gate(fake_llm, monkeypatch)
    refresh = client.app.state.profile_refresh
    con = mdb.connect(settings.db_path)
    a, b, c = _facts(con, settings)
    _build(con, settings, fake_llm)
    manual = client.post("/v1/summary/regenerate").json()["job_id"]
    assert gate.entered.wait(10)          # chose a, b and c
    assert client.delete(f"/v1/facts/{a}").status_code == 200
    time.sleep(0.2)                       # the refresh is blocked, not built
    assert gate.drafts == 2
    gate.release.set()
    assert refresh.wait(10)
    assert _done(manual)["status"] == "ok"
    assert gate.drafts == 3
    assert sorted(summary.get(con)["source_fact_ids"]) == [b, c]
    con.close()


# ---- failures and restarts ----

def test_a_failed_rebuild_keeps_the_old_profile(client, settings, fake_llm):
    con = mdb.connect(settings.db_path)
    a, b, c = _facts(con, settings)
    cited = _build(con, settings, fake_llm)
    fake_llm["fail_when_empty"] = True     # the next model call raises
    ledger.quarantine_many(con, [a], "hold")
    job = client.app.state.profile_refresh.now()
    assert _done(job)["status"] == "failed"
    assert summary.get(con)["source_fact_ids"] == cited
    assert summary.get(con)["summary"] == PROFILE
    con.close()


def test_startup_arms_a_rebuild_for_a_profile_that_cites_a_held_fact(
        settings, fake_llm):
    clean = create_app(settings)
    assert not clean.state.profile_refresh.waiting()
    con = mdb.connect(settings.db_path)
    a, b, c = _facts(con, settings)
    _build(con, settings, fake_llm)
    ledger.quarantine_many(con, [a], "held while the service was down")
    con.close()
    restarted = create_app(settings)
    assert restarted.state.profile_refresh.waiting()
