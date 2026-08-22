"""The owner admin token/session for exact-row endpoints (admin-gate v2/v3/v4).

v2 gated `GET /v1/facts` / `GET /v1/review` (and every verb on one existing
fact by id) behind an admin token required even on loopback — but embedded
that same token in the HTML of an UNAUTHENTICATED `GET /`, so a sandboxed
process could fetch the page, parse the token, and use it — the identical
bypass, one hop removed. v3 fixed that: `GET /` never hands out a credential,
serving the real UI only once the browser holds a session cookie, set
exclusively by `POST /login` after the OWNER supplies the token (out of
band — the terminal at startup, or their own `MEMORY_AUTH_TOKEN`).

v3's cookie held the real bearer token itself, though: `HttpOnly` stopped
page JS reading it, but ANY same-origin request still carried a reusable,
non-expiring, non-revocable credential — a copied cookie was forever
equivalent to the token, and "logout" could only ever clear one browser's
copy. v4 replaces it with an opaque, random, server-tracked session id: the
cookie value is NOT the token, sessions expire, and logout is a true
server-side revocation that invalidates every copy at once, not just the
browser that clicked it.

These tests prove: an unauthenticated loopback caller can fetch no usable
credential anywhere; the login flow genuinely works; the session cookie is
provably not the bearer token; a copied session works until revoked, and
logout revokes it EVERYWHERE (not just the logging-out client); an expired
session is rejected; login never adopts a client-supplied session id (no
fixation); and the rest of the surface is unaffected.
"""

import pytest
from fastapi.testclient import TestClient

from memory_service import db
from memory_service.api import create_app
from memory_service.config import Settings


def _app(tmp_path, **kw):
    return create_app(Settings(data_dir=tmp_path / "data", **kw))


def _client(app, token=None, base_url="http://127.0.0.1"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, base_url=base_url, headers=headers)


# Since the password-login slice: the everyday browser login is now a durable PASSWORD, not the
# admin token. The admin token is the out-of-band RECOVERY SECRET that gates
# first-run enrollment/reset. These helpers set a password once (recovery-gated)
# so the login-flow tests below can then log in with it.
PASSWORD = "correct-horse-battery-staple"


def _enroll(app, password=PASSWORD):
    """First-run enrollment, proving ownership with the recovery secret (the
    admin token — never the everyday login). Returns the enrolled password."""
    setup = TestClient(app, base_url="http://127.0.0.1")
    r = setup.post("/enroll", data={"recovery": app.state.admin_token,
                                    "password": password, "confirm": password},
                   follow_redirects=False)
    assert r.status_code == 303
    return password


# ── the core gap, closed: no token, no exact rows, even on loopback ─────────
@pytest.mark.parametrize("method,path,kw", [
    ("get", "/v1/facts", {}),
    ("get", "/v1/review", {}),
    # Transcript gating: verbatim transcript search, job reads (a consolidate job's
    # result embeds fact rows), and the consolidate trigger are all
    # exact-row surfaces too.
    ("post", "/v1/search", {"json": {"query": "x"}}),
    ("get", "/v1/jobs/any-id", {}),
    ("post", "/v1/consolidate", {}),
])
def test_unauthenticated_loopback_read_is_denied(tmp_path, method, path, kw):
    app = _app(tmp_path)
    c = _client(app, token=None)
    r = getattr(c, method)(path, **kw)
    assert r.status_code == 401
    assert "error" in r.json()


def test_authenticated_loopback_read_is_allowed(tmp_path):
    app = _app(tmp_path)
    c = _client(app, token=app.state.admin_token)
    assert c.get("/v1/facts").status_code == 200
    assert c.get("/v1/review").status_code == 200


def test_wrong_token_is_refused_not_just_missing_one(tmp_path):
    app = _app(tmp_path)
    c = _client(app, token="not-the-real-token")
    assert c.get("/v1/facts").status_code == 401


# ── the same gate covers every verb on ONE existing fact by id ──────────────
def test_mutate_by_id_endpoints_require_the_token_too(tmp_path):
    app = _app(tmp_path)
    owner = _client(app, token=app.state.admin_token)
    stranger = _client(app, token=None)

    fid = owner.post("/v1/facts", json={"content": "Alex adopted a rescue greyhound."}).json()["id"]
    fid2 = owner.post("/v1/facts", json={"content": "Alex renamed the greyhound Biscuit."}).json()["id"]

    assert stranger.patch(f"/v1/facts/{fid}", json={"confidence": "low"}).status_code == 401
    assert stranger.post(f"/v1/facts/{fid}/supersede", json={"successor_id": fid2}).status_code == 401
    assert stranger.post(f"/v1/facts/{fid}/approve").status_code == 401
    assert stranger.post(f"/v1/facts/{fid}/dismiss").status_code == 401
    assert stranger.delete(f"/v1/facts/{fid}").status_code == 401
    assert stranger.post("/v1/review/dismiss-all").status_code == 401  #
    assert stranger.post("/v1/facts/quarantine",
                         json={"ids": [fid], "reason": "x"}).status_code == 401  #

    # the owner, with the right token, can do all of it
    assert owner.patch(f"/v1/facts/{fid}", json={"confidence": "low"}).status_code == 200
    assert owner.post(f"/v1/facts/{fid}/supersede", json={"successor_id": fid2}).status_code == 200
    assert owner.post("/v1/review/dismiss-all").status_code == 200
    assert owner.post("/v1/facts/quarantine",
                      json={"ids": [fid], "reason": "malformed"}).status_code == 200
    assert owner.delete(f"/v1/facts/{fid2}").status_code == 200


# ── scoped tightening: everything else is unaffected on loopback ───────────
def test_create_and_general_endpoints_stay_open_on_loopback(tmp_path, fake_llm):
    app = _app(tmp_path)
    c = _client(app, token=None)
    assert c.get("/v1/health").status_code == 200
    assert c.post("/v1/facts", json={"content": "Alex bakes sourdough weekly."}).status_code == 200
    assert c.post("/v1/recall", json={"query": "sourdough"}).status_code == 200
    # /v1/search moved to the gated list with transcript gating — recall stays the
    # open chat-loop surface; search returns verbatim transcripts.
    assert c.get("/v1/summary").status_code == 200


def test_attachment_routes_require_the_owner_credential(tmp_path):
    """Attachments expose exact fact rows, the source message body and up to
    4000 chars of document text, plus a hard delete — so they need the owner
    credential on loopback exactly like /v1/facts.

    This closes a real bypass: a sandboxed process sharing 127.0.0.1 could read
    ledger content through /v1/attachments/{id}/preview while /v1/facts
    correctly refused it, and could destroy rows through the open DELETE."""
    app = _app(tmp_path)
    anon = _client(app, token=None)
    owner = _client(app, token=app.state.admin_token)

    # every attachment route refuses an unauthenticated loopback caller...
    for path in ("/v1/attachments",
                 "/v1/attachments/1/file",
                 "/v1/attachments/1/preview"):
        assert anon.get(path).status_code == 401, path
    assert anon.delete("/v1/attachments/1").status_code == 401

    # ...and the owner gets through the gate (404 here = no such attachment,
    # which proves the credential was accepted rather than refused).
    assert owner.get("/v1/attachments").status_code == 200
    for path in ("/v1/attachments/1/file", "/v1/attachments/1/preview"):
        assert owner.get(path).status_code == 404, path
    assert owner.delete("/v1/attachments/1").status_code == 404


def test_open_recall_projects_only_contract_fields(tmp_path, fake_llm):
    """/v1/recall is open by design — every chat round calls it — so WHAT IT
    PROJECTS is the security boundary. It must return only the fields
    docs/API.md promises, never the whole ledger row: content_hash,
    conversation_id, source_message_id and the quarantine bookkeeping are
    internals, and handing them to any loopback caller made the gate on
    /v1/facts decorative."""
    app = _app(tmp_path)
    c = _client(app, token=None)
    c.post("/v1/facts", json={"content": "Alex leases a workshop on Foundry Lane."})

    facts = c.post("/v1/recall", json={"query": "workshop"}).json()["facts"]
    assert facts, "expected a hit"
    got = set(facts[0])
    assert got <= {"id", "content", "event_date", "confidence",
                   "origin_agent", "score"}, f"leaked: {got}"
    # the fields consumers actually render must survive the projection
    assert {"id", "content"} <= got
    for leaked in ("content_hash", "conversation_id", "source_message_id",
                   "quarantine_reason", "quarantined_at", "review_dismissed_at",
                   "superseded_by", "embedding"):
        assert leaked not in got, leaked


def test_recall_limit_is_bounded_against_whole_ledger_export(tmp_path, fake_llm):
    """An unbounded top-N over an empty query is an export primitive, not a
    recall: 500 was enough to dump a ledger in one unauthenticated call."""
    app = _app(tmp_path)
    c = _client(app, token=None)
    assert c.post("/v1/recall", json={"query": "", "limit": 500}).status_code == 422
    assert c.post("/v1/recall", json={"query": "", "limit": 50}).status_code == 200


# ── the token itself: stable if configured, ephemeral (and non-shared) if not
def test_configured_auth_token_becomes_the_stable_admin_token(tmp_path):
    app = _app(tmp_path, auth_token="owner-chosen-value")
    assert app.state.admin_token == "owner-chosen-value"


def test_unconfigured_token_is_random_and_differs_per_process(tmp_path):
    app1 = _app(tmp_path / "a")
    app2 = _app(tmp_path / "b")
    assert app1.state.admin_token != app2.state.admin_token
    assert len(app1.state.admin_token) >= 32


# ── admin-gate v3: GET / must never hand a credential to an unauthenticated caller ─
def test_unauthenticated_root_serves_locked_page_with_no_credential(tmp_path):
    app = _app(tmp_path)
    c = _client(app, token=None)
    r = c.get("/")
    assert r.status_code == 200  # a page IS served — just not the real UI
    html = r.text
    # the real admin page (facts table, review queue JS) must be absent...
    assert "fetch(" not in html
    # ...and nowhere does the actual token value appear in this response
    assert app.state.admin_token not in html
    assert "locked" in html.lower()


def test_root_with_wrong_cookie_stays_locked(tmp_path):
    app = _app(tmp_path)
    c = _client(app, token=None)
    c.cookies.set("mm_admin", "not-a-real-session-id")
    assert "locked" in c.get("/").text.lower()


# ── the login flow: the everyday login is now the enrolled PASSWORD ─────────
def test_login_with_correct_password_sets_cookie_and_unlocks(tmp_path):
    app = _app(tmp_path)
    _enroll(app)
    c = _client(app, token=None)
    r = c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    assert c.cookies.get("mm_admin")
    # the cookie the browser now holds genuinely unlocks the UI and the API
    assert "fetch(" in c.get("/").text
    assert c.get("/v1/facts").status_code == 200
    assert c.get("/v1/review").status_code == 200


def test_login_with_wrong_password_is_refused_and_sets_no_cookie(tmp_path):
    app = _app(tmp_path)
    _enroll(app)
    c = _client(app, token=None)
    r = c.post("/login", data={"password": "guess-the-wrong-one"}, follow_redirects=False)
    assert r.status_code == 401
    assert "mm_admin" not in c.cookies
    assert c.get("/v1/facts").status_code == 401


def test_admin_token_is_not_accepted_as_the_everyday_password(tmp_path):
    """The recovery secret must NOT double as the everyday login: even
    the correct admin token, submitted to /login as a password, is refused."""
    app = _app(tmp_path)
    _enroll(app)
    c = _client(app, token=None)
    r = c.post("/login", data={"password": app.state.admin_token}, follow_redirects=False)
    assert r.status_code == 401
    assert c.get("/v1/facts").status_code == 401


def test_login_cookie_is_httponly_and_samesite_strict(tmp_path):
    app = _app(tmp_path)
    _enroll(app)
    c = _client(app, token=None)
    r = c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    set_cookie = r.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()
    assert "max-age" in set_cookie.lower()  # a bounded TTL, not a forever-cookie


def test_logout_clears_the_cookie(tmp_path):
    app = _app(tmp_path)
    _enroll(app)
    c = _client(app, token=None)
    c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert c.get("/v1/facts").status_code == 200
    c.post("/logout", follow_redirects=False)
    assert "locked" in c.get("/").text.lower()
    assert c.get("/v1/facts").status_code == 401


# ── the login page itself gives away nothing (no token/verifier, no oracle) ─
def test_locked_page_never_contains_the_recovery_secret_even_as_a_hint(tmp_path):
    app = _app(tmp_path, auth_token="a-very-specific-owner-value")
    _enroll(app)
    c = _client(app, token=None)
    assert "a-very-specific-owner-value" not in c.get("/").text
    r = c.post("/login", data={"password": "wrong-guess"}, follow_redirects=False)
    assert "a-very-specific-owner-value" not in r.text


# ── admin-gate v4: the cookie is an opaque session id, NOT the bearer token ────────
def test_session_cookie_value_is_not_the_bearer_token(tmp_path):
    app = _app(tmp_path, auth_token="a-very-specific-owner-value")
    _enroll(app)
    c = _client(app, token=None)
    c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    sid = c.cookies.get("mm_admin")
    assert sid is not None
    assert sid != "a-very-specific-owner-value"
    assert sid != PASSWORD
    assert len(sid) >= 32  # genuinely high-entropy, not a short/guessable id
    # and using the raw token itself AS a cookie value must NOT authenticate
    other = _client(app, token=None)
    other.cookies.set("mm_admin", "a-very-specific-owner-value")
    assert other.get("/v1/facts").status_code == 401


def test_session_is_tracked_server_side(tmp_path):
    app = _app(tmp_path)
    _enroll(app)
    c = _client(app, token=None)
    c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    sid = c.cookies.get("mm_admin")
    assert sid in app.state.admin_sessions
    assert app.state.admin_sessions[sid] > 0  # an expiry timestamp is recorded


def test_logout_revokes_the_session_for_every_copy_not_just_one_browser(tmp_path):
    """The exact scenario a stolen/copied cookie represents: a second client
    presents the SAME session id. v3 could only clear one browser's cookie;
    v4's logout deletes the session server-side, so both are cut off at once."""
    app = _app(tmp_path)
    _enroll(app)
    owner = _client(app, token=None)
    owner.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    sid = owner.cookies.get("mm_admin")

    thief = _client(app, token=None)
    thief.cookies.set("mm_admin", sid)
    assert thief.get("/v1/facts").status_code == 200  # the copy works, pre-logout

    owner.post("/logout", follow_redirects=False)

    assert thief.get("/v1/facts").status_code == 401  # revoked for the copy too
    assert owner.get("/v1/facts").status_code == 401
    assert sid not in app.state.admin_sessions


def test_expired_session_is_rejected(tmp_path):
    app = _app(tmp_path)
    _enroll(app)
    c = _client(app, token=None)
    c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    sid = c.cookies.get("mm_admin")
    assert c.get("/v1/facts").status_code == 200

    app.state.admin_sessions[sid] = db.now() - 1  # force it into the past
    assert c.get("/v1/facts").status_code == 401
    assert c.get("/").status_code == 200 and "locked" in c.get("/").text.lower()
    # an expired session is evicted, not just rejected-in-place
    assert sid not in app.state.admin_sessions


def test_login_ignores_any_preexisting_cookie_no_fixation(tmp_path):
    """A caller can't hand the server a session id to adopt — login always
    mints its own, so pre-seeding a cookie before authenticating buys nothing
    (the classic session-fixation attack: pre-set a known id, wait for the
    victim to log in under it, then use that known id yourself)."""
    app = _app(tmp_path)
    _enroll(app)
    planted = "attacker-chosen-session-id"
    # the attacker "pre-seeds" the credential the server would have to adopt
    # for fixation to work: the planted id itself must never be authoritative,
    # before OR after some other login happens.
    victim = _client(app, token=None)
    victim.cookies.set("mm_admin", planted)
    r = victim.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert planted not in app.state.admin_sessions
    # the server's own Set-Cookie response must carry a freshly minted value,
    # never the planted one
    assert planted not in r.headers.get("set-cookie", "")

    stranger = _client(app, token=None)
    stranger.cookies.set("mm_admin", planted)
    assert stranger.get("/v1/facts").status_code == 401


def test_mcp_style_bearer_access_is_unaffected_by_session_lifecycle(tmp_path):
    """The opt-in read-only MCP admin server keeps using the real bearer
    token directly — logging in/out of the browser session, or letting a
    browser session expire, must never touch this path."""
    app = _app(tmp_path)
    _enroll(app)
    bearer = _client(app, token=app.state.admin_token)
    assert bearer.get("/v1/facts").status_code == 200

    browser = _client(app, token=None)
    browser.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    browser.post("/logout", follow_redirects=False)

    assert bearer.get("/v1/facts").status_code == 200
    assert bearer.get("/v1/review").status_code == 200
