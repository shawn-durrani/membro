"""The integrity verdict answers from cache, not from a live scan.

`PRAGMA quick_check` scans the whole database file; running it inside
/v1/health put that scan on the chat client's round-critical path. These
tests pin the replacement contract: the verdict is computed synchronously
exactly once when cold, every later health read answers instantly from the
cache, staleness refreshes in the BACKGROUND while still answering with the
last verdict, and a bad verdict still degrades /v1/health.
"""

import time

import pytest
from fastapi.testclient import TestClient

from memory_service import db as db_mod
from memory_service.api import create_app


@pytest.fixture(autouse=True)
def _reset_integrity_cache():
    with db_mod._integrity_lock:
        db_mod._integrity.update(verdict=None, checked_at=0.0, refreshing=False)
    yield
    with db_mod._integrity_lock:
        db_mod._integrity.update(verdict=None, checked_at=0.0, refreshing=False)


@pytest.fixture
def counting_refresh(monkeypatch):
    calls = {"n": 0}
    real = db_mod._refresh_integrity

    def counted(settings):
        calls["n"] += 1
        return real(settings)

    monkeypatch.setattr(db_mod, "_refresh_integrity", counted)
    return calls


def test_cold_verdict_computes_once_then_serves_from_cache(settings, con, counting_refresh):
    assert db_mod.integrity_verdict(settings) == "ok"
    assert counting_refresh["n"] == 1
    # within the TTL: no rescan, same verdict, instantly
    assert db_mod.integrity_verdict(settings) == "ok"
    assert db_mod.health(settings)["integrity"] == "ok"
    assert counting_refresh["n"] == 1


def test_stale_verdict_answers_immediately_and_refreshes_in_background(
        settings, con, counting_refresh):
    assert db_mod.integrity_verdict(settings) == "ok"
    # age the cache past the TTL and plant a marker verdict: the next read
    # must return the OLD verdict (no blocking) and refresh behind it
    with db_mod._integrity_lock:
        db_mod._integrity["checked_at"] = time.time() - db_mod.INTEGRITY_TTL_S - 1
        db_mod._integrity["verdict"] = "stale-marker"
    assert db_mod.integrity_verdict(settings) == "stale-marker"
    deadline = time.time() + 5
    while time.time() < deadline:
        with db_mod._integrity_lock:
            if db_mod._integrity["verdict"] == "ok":
                break
        time.sleep(0.02)
    assert db_mod._integrity["verdict"] == "ok"  # background refresh landed
    assert counting_refresh["n"] == 2


def test_bad_verdict_still_degrades_health(settings, fake_llm):
    app = create_app(settings)  # warms the verdict at startup
    client = TestClient(app, base_url="http://127.0.0.1")
    with db_mod._integrity_lock:
        db_mod._integrity["verdict"] = "row 17 missing from index"
        db_mod._integrity["checked_at"] = time.time()  # fresh: no re-scan
    body = client.get("/v1/health").json()
    assert body["status"] == "degraded"
    assert body["db"]["integrity"] == "row 17 missing from index"


def test_startup_warms_the_verdict_so_probes_never_scan(settings, fake_llm, counting_refresh):
    create_app(settings)
    assert counting_refresh["n"] == 1  # paid once, at startup
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    client.get("/v1/health")
    # neither the second create_app (verdict still fresh, within TTL) nor
    # the probe itself triggered another scan
    assert counting_refresh["n"] == 1
