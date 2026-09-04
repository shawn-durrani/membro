"""Contract 1.4 (#84): the additive fields a client app consumes, and the
calendar-day rule for event_date. Every one is additive on 1.3, so the
older assertions in test_api_contract.py keep passing beside these."""

import datetime

import pytest
from fastapi.testclient import TestClient

from memory_service import db, ledger, recall
from memory_service.api import create_app


def _client(settings):
    app = create_app(settings)
    return TestClient(app, base_url="http://127.0.0.1",
                      headers={"Authorization": f"Bearer {app.state.admin_token}"})


@pytest.fixture
def client(settings, fake_llm):
    return _client(settings)


def _ingest(client, conv, msgs):
    r = client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": conv,
        "title": "t", "messages": msgs})
    assert r.status_code == 200, r.text
    return r.json()


def _msg(ext, text, **extra):
    return {"external_id": ext, "speaker": "user", "content": text,
            "created_at": "2026-07-01T10:00:00+10:00", **extra}


# ---- handshake ----

def test_health_speaks_at_least_1_4_and_reports_loopback_origin_by_default(client, settings):
    h = client.get("/v1/health").json()
    # 1.5 (#93) is additive over 1.4: the version only ever moves up.
    major, minor = h["contract_version"].split(".")
    assert (int(major), int(minor)) >= (1, 4)
    assert h["browser_origin"] == f"http://127.0.0.1:{settings.port}"


def test_browser_origin_follows_the_trusted_tailnet_host(settings, fake_llm):
    served = settings.model_copy(update={"trusted_hosts": ["mac.example.ts.net"]})
    h = _client(served).get("/v1/health").json()
    assert h["browser_origin"] == "https://mac.example.ts.net:8443"
    custom = settings.model_copy(update={"trusted_hosts": ["mac.example.ts.net"],
                                         "tailscale_port": 9443})
    assert custom.reachable_browser_origin() == "https://mac.example.ts.net:9443"


def test_browser_origin_explicit_setting_wins_and_loses_its_slash(settings):
    explicit = settings.model_copy(update={"browser_origin": "https://memory.example.net/",
                                           "trusted_hosts": ["other.host"]})
    assert explicit.reachable_browser_origin() == "https://memory.example.net"


# ---- search carries provenance back out ----

def test_search_hits_carry_web_sources(client):
    _ingest(client, "c-web", [
        _msg("1", "the kakapo is a flightless parrot",
             web_sources=["example.com", "birds.example.org"]),
        _msg("2", "the kakapo lives in New Zealand"),
    ])
    hits = client.post("/v1/search", json={"query": "kakapo", "limit": 10}).json()["hits"]
    by_content = {h["content"]: h for h in hits}
    assert len(hits) == 2
    for h in hits:
        assert isinstance(h["web_sources"], list)
    stamped = [h for h in hits if h["web_sources"]]
    assert len(stamped) == 1
    assert stamped[0]["web_sources"] == ["example.com", "birds.example.org"]
    assert [h for h in hits if not h["web_sources"]][0]["web_sources"] == []


def test_like_fallback_hits_carry_web_sources(con, settings):
    from memory_service import episodic
    episodic.ingest(con, "multi-model-chat", "c-fb", [
        {"external_id": "1", "speaker": "user", "content": "weka on the track",
         "created_at": 1700000000.0, "web_sources": ["example.com"]}])
    con.commit()
    hits = episodic._like_fallback(con, ["weka"], 5)
    assert hits and hits[0]["web_sources"] == ["example.com"]


# ---- the per-conversation watermark ----

def test_watermark_unknown_conversation_is_404_in_the_envelope(client):
    r = client.get("/v1/conversations/multi-model-chat/nope/watermark")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "404"


def test_watermark_reports_the_numerically_highest_id(client):
    _ingest(client, "c-wm", [_msg("9", "nine"), _msg("10", "ten")])
    r = client.get("/v1/conversations/multi-model-chat/c-wm/watermark")
    assert r.status_code == 200
    assert r.json() == {"highest_external_id": "10", "messages": 2}


def test_watermark_is_open_on_loopback_without_the_owner_token(settings, fake_llm):
    app = create_app(settings)
    anon = TestClient(app, base_url="http://127.0.0.1")
    anon.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "c-open",
        "messages": [_msg("3", "three")]})
    r = anon.get("/v1/conversations/multi-model-chat/c-open/watermark")
    assert r.status_code == 200
    assert r.json()["highest_external_id"] == "3"


def test_watermark_counts_an_erased_message_as_held(client):
    """The owner erased the newest message. The watermark must not drop
    below it, or the client would re-send the exact message just erased."""
    _ingest(client, "c-er", [_msg("20", "keep this"), _msg("21", "erase this")])
    ref = client.get("/v1/messages/resolve", params={
        "source_app": "multi-model-chat", "conversation": "c-er", "message": "21"})
    assert ref.status_code == 200, ref.text
    mid = ref.json()["id"]
    assert client.delete(f"/v1/messages/{mid}").status_code == 200
    r = client.get("/v1/conversations/multi-model-chat/c-er/watermark").json()
    assert r == {"highest_external_id": "21", "messages": 1}


# ---- event_date is a calendar day ----

def _local_midnight(y, m, d):
    return datetime.datetime(y, m, d).timestamp()


def _stored(client, fact_id):
    facts = client.get("/v1/facts", params={"status": "all", "limit": 1000}).json()["facts"]
    return next(f for f in facts if f["id"] == fact_id)


def test_saved_fact_event_date_is_local_midnight_of_its_day(client):
    r = client.post("/v1/facts", json={"content": "signed the lease on the fourth",
                                       "event_date": "2026-07-04"})
    assert r.status_code == 200, r.text
    assert _stored(client, r.json()["id"])["event_date"] == _local_midnight(2026, 7, 4)


def test_saved_fact_timestamp_collapses_to_its_local_day(client):
    noon_local = datetime.datetime(2026, 7, 4, 12, 0).astimezone().isoformat()
    r = client.post("/v1/facts", json={"content": "lunch meeting about the lease",
                                       "event_date": noon_local})
    assert _stored(client, r.json()["id"])["event_date"] == _local_midnight(2026, 7, 4)


def test_fact_without_event_date_gets_the_day_it_was_saved(con, settings):
    added = ledger.add_fact(con, "a fact saved just now", settings)
    f = dict(con.execute("SELECT event_date, created_at FROM facts WHERE id=?",
                         (added["id"],)).fetchone())
    assert f["event_date"] == db.day_start(f["created_at"])
    assert f["event_date"] <= f["created_at"] < f["event_date"] + 86400


def test_day_start_is_idempotent_and_local():
    ts = datetime.datetime(2026, 7, 4, 23, 59, 59).timestamp()
    assert db.day_start(ts) == _local_midnight(2026, 7, 4)
    assert db.day_start(db.day_start(ts)) == db.day_start(ts)


def test_recall_breaks_a_same_day_tie_on_the_save_time(con, settings):
    day = _local_midnight(2026, 7, 4)
    older = ledger.add_fact(con, "kea parrot sighting one", settings, event_date=day)
    newer = ledger.add_fact(con, "kea parrot sighting two", settings, event_date=day)
    con.execute("UPDATE facts SET created_at=? WHERE id=?", (day + 100, older["id"]))
    con.execute("UPDATE facts SET created_at=? WHERE id=?", (day + 200, newer["id"]))
    con.commit()
    got = recall.recall(con, settings, "kea parrot sighting", limit=5)
    assert [g["id"] for g in got][:2] == [newer["id"], older["id"]]
