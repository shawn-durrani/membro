"""The localhost trust boundary — DNS-rebinding defense + a real auth token."""

import pytest
from fastapi.testclient import TestClient

from memory_service.api import create_app


def test_rebound_host_is_refused(settings):
    client = TestClient(create_app(settings), base_url="http://evil.example.com")
    r = client.get("/v1/health")
    assert r.status_code == 403


def test_loopback_hosts_allowed(settings):
    app = create_app(settings)
    for base in ("http://127.0.0.1", "http://localhost"):
        assert TestClient(app, base_url=base).get("/v1/health").status_code == 200


def test_token_grants_nonlocal_access_and_is_checked(tmp_path):
    from memory_service.config import Settings
    s = Settings(data_dir=tmp_path / "d", auth_token="s3cret")
    app = create_app(s)
    ext = TestClient(app, base_url="http://mem.example.com")
    assert ext.get("/v1/health").status_code == 403                      # no token
    assert ext.get("/v1/health",
                   headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert ext.get("/v1/health",
                   headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_ingest_message_cap(settings):
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    msgs = [{"external_id": str(i), "speaker": "user", "content": "x",
             "created_at": "2026-07-01T10:00:00"} for i in range(5001)]
    r = client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "big", "messages": msgs})
    assert r.status_code == 413


def test_read_limits_are_bounded(settings):
    from fastapi.testclient import TestClient
    from memory_service.api import create_app
    app = create_app(settings)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.post("/v1/recall", json={"query": "x", "limit": 99999}).status_code == 422
    # Admin gate + transcript gating: /v1/facts and /v1/search require the owner admin token
    # even on loopback — send it so these assertions still test the LIMIT
    # bound, not the (separately tested) auth gate.
    auth = {"Authorization": f"Bearer {app.state.admin_token}"}
    assert c.post("/v1/search", json={"query": "x", "limit": 99999},
                  headers=auth).status_code == 422
    assert c.get("/v1/facts", params={"limit": 99999}, headers=auth).status_code == 422


def test_post_facts_quarantines_model_origin(settings):
    """A model-origin write over POST /v1/facts must land in review, not
    canon — the HTTP route inherits the write gate, and this pins it so the
    boundary can't silently regress (the live-service symptom that exposed the hole).
    A plain 'user' write, and an mcp:* origin that names a trusted source_app,
    are both exercised to prove the gate keys on the write, not a claimed app."""
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")

    held = client.post("/v1/facts", json={
        "content": "Alex is drafting a novel about lighthouses.",
        "origin_agent": "claude"})
    assert held.status_code == 200
    assert held.json()["quarantined"] is True

    laundered = client.post("/v1/facts", json={
        "content": "Alex owns a private island in the Pacific.",
        "origin_agent": "mcp:claude-code", "source_app": "multi-model-chat"})
    assert laundered.status_code == 200
    assert laundered.json()["quarantined"] is True

    canon = client.post("/v1/facts", json={
        "content": "Alex prefers tea over coffee.", "origin_agent": "user"})
    assert canon.status_code == 200
    assert canon.json()["quarantined"] is False


def test_fact_content_length_capped(con, settings):
    from memory_service import ledger
    import pytest
    with pytest.raises(ValueError, match="too long"):
        ledger.add_fact(con, "x " * 6000, settings)


def test_memory_port_env_reaches_the_app(monkeypatch):
    """start.sh's port guard and the app must agree on MEMORY_PORT — found by
    the cold-install rehearsal binding 8901 while the guard checked 8916."""
    from memory_service.config import load_settings
    monkeypatch.setenv("MEMORY_PORT", "8916")
    assert load_settings().port == 8916


# ---------- trusted hosts: the browser surface over a tailnet ----------
#
# WHY this exists: a browser cannot attach an Authorization header to a
# navigation, so the token rule above made the owner's own phone — over the
# one sanctioned non-loopback path — unable to reach even the lock screen,
# leaving the owner password gate unreachable from the device it exists for.
# These tests pin the narrow widening: the LOGIN surface and an
# already-authenticated session, never anonymous access to fact data.

TAILNET = "https://my-mac.my-tailnet.ts.net"  # documented placeholder shape


def _trusted_app(tmp_path, **extra):
    from memory_service.config import Settings
    return create_app(Settings(data_dir=tmp_path / "d",
                               trusted_hosts=["my-mac.my-tailnet.ts.net"],
                               **extra))


def test_trusted_hosts_defaults_to_empty_so_nothing_widens_by_itself(settings):
    assert settings.trusted_hosts == []
    # a tailnet-shaped host is refused exactly like any other non-loopback one
    assert TestClient(create_app(settings), base_url=TAILNET) \
        .get("/v1/health").status_code == 403


def test_trusted_host_reaches_the_lock_screen_but_not_the_ledger(tmp_path):
    c = TestClient(_trusted_app(tmp_path), base_url=TAILNET)
    assert c.get("/").status_code == 200            # the lock screen renders
    assert c.post("/login", data={"password": "nope"}).status_code in (200, 401, 403)
    # …and nothing that carries memory is readable without signing in
    for probe in (lambda: c.get("/v1/health"),
                  lambda: c.post("/v1/recall", json={"query": "x"}),
                  lambda: c.post("/v1/search", json={"query": "x"}),
                  lambda: c.get("/v1/summary"),
                  lambda: c.get("/v1/facts")):
        assert probe().status_code == 403


def test_trusted_host_with_a_live_session_may_use_the_app(tmp_path):
    app = _trusted_app(tmp_path)
    c = TestClient(app, base_url=TAILNET)
    sid = "test-session-id"
    app.state.admin_sessions[sid] = 1e12  # a session this process minted
    c.cookies.set("mm_admin", sid)
    assert c.get("/v1/health").status_code == 200
    # an expired session is not a session
    app.state.admin_sessions[sid] = 1.0
    assert c.get("/v1/health").status_code == 403


def test_trusted_host_still_honours_the_bearer_token(tmp_path):
    c = TestClient(_trusted_app(tmp_path, auth_token="s3cret"), base_url=TAILNET)
    assert c.get("/v1/health",
                 headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/v1/health",
                 headers={"Authorization": "Bearer wrong"}).status_code == 403


def test_cross_site_requests_are_refused_even_on_a_trusted_host(tmp_path):
    """A malicious page that knows the tailnet name still cannot drive this
    API from its own origin."""
    app = _trusted_app(tmp_path)
    c = TestClient(app, base_url=TAILNET)
    sid = "test-session-id"
    app.state.admin_sessions[sid] = 1e12
    c.cookies.set("mm_admin", sid)
    assert c.get("/v1/health",
                 headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    # a same-origin browser request (and a header-less curl) is unaffected
    assert c.get("/v1/health",
                 headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_an_untrusted_host_is_still_refused_with_trusted_hosts_configured(tmp_path):
    c = TestClient(_trusted_app(tmp_path), base_url="https://evil.example.com")
    assert c.get("/").status_code == 403
    assert c.get("/v1/health").status_code == 403


def test_env_var_parses_a_comma_separated_list(monkeypatch):
    from memory_service.config import load_settings
    monkeypatch.setenv("MEMORY_TRUSTED_HOSTS",
                       " My-Mac.My-Tailnet.ts.net , other.my-tailnet.ts.net ,")
    assert load_settings().trusted_hosts == ["my-mac.my-tailnet.ts.net",
                                             "other.my-tailnet.ts.net"]
