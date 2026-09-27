"""Owner-password enrollment / login / reset — the password-login delivery slice.

The everyday admin login used to be the process's admin token: ephemeral by
default (a fresh one every restart) and doubling as the MCP/curl bearer
credential — so ordinary browser login demanded terminal access after every
restart. Slice 1 replaces it with a durable password:

- A memory-hard scrypt verifier persists in the local store, so the SAME
  password unlocks a FRESH app instance on the same data directory (survives
  restart). Since #131 the opaque sessions survive a restart too, stored as
  hashes, and a reset ends every one of them.
- First-run enrollment and reset require the out-of-band RECOVERY SECRET (the
  admin token). An unauthenticated caller sharing loopback — a sandboxed coding
  agent — cannot self-enroll: it never sees the terminal/.env secret.
- The admin token is NOT accepted as the everyday login, and neither it nor the
  verifier ever appears in any unauthenticated response.
- The MCP/curl Bearer path and exact-row gating are untouched.

These are the slice-1 acceptance tests; the admin-gate session mechanics live in
test_admin_auth.py.
"""

import pytest
from fastapi.testclient import TestClient

from memory_service import auth, db
from memory_service.api import create_app
from memory_service.config import Settings

PASSWORD = "a-durable-owner-passphrase"
NEW_PASSWORD = "an-entirely-different-one"


def _app(tmp_path, **kw):
    # A fixed data_dir under tmp_path so a second create_app() can reopen the
    # SAME database — the crux of the persistence tests.
    return create_app(Settings(data_dir=tmp_path / "data", **kw))


def _client(app, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, base_url="http://127.0.0.1", headers=headers)


def _enroll(app, c, recovery=None, password=PASSWORD, confirm=None):
    return c.post("/enroll", data={
        "recovery": app.state.admin_token if recovery is None else recovery,
        "password": password,
        "confirm": password if confirm is None else confirm,
    }, follow_redirects=False)


# ── the scrypt verifier itself: durable shape, no plaintext, constant-time ──
def test_verifier_roundtrips_and_stores_no_plaintext():
    stored = auth.hash_password(PASSWORD)
    assert auth.verify_password(PASSWORD, stored)
    assert not auth.verify_password("wrong", stored)
    # nothing reversible: the password never appears in the persisted verifier
    assert PASSWORD not in stored
    assert "scrypt" in stored


def test_same_password_hashes_differ_each_time_random_salt():
    a = auth.hash_password(PASSWORD)
    b = auth.hash_password(PASSWORD)
    assert a != b  # distinct random salts
    assert auth.verify_password(PASSWORD, a) and auth.verify_password(PASSWORD, b)


def test_empty_or_garbage_verifier_is_a_clean_false():
    assert not auth.verify_password(PASSWORD, "")
    assert not auth.verify_password(PASSWORD, "not-json")
    assert not auth.verify_password(PASSWORD, '{"alg":"bogus"}')


# ── first-run enrollment is recovery-gated ──────────────────────────────────
def test_fresh_install_shows_enrollment_not_login(tmp_path):
    app = _app(tmp_path)
    c = _client(app)
    html = c.get("/").text.lower()
    assert "first-time setup" in html
    assert "/enroll" in html


def test_enroll_with_recovery_secret_sets_password_and_logs_in(tmp_path):
    app = _app(tmp_path)
    c = _client(app)
    r = _enroll(app, c)
    assert r.status_code == 303
    assert c.cookies.get("mm_admin")  # logged straight in
    assert c.get("/v1/facts").status_code == 200
    # and the password now works for a plain login on a separate client
    c2 = _client(app)
    assert c2.post("/login", data={"password": PASSWORD},
                   follow_redirects=False).status_code == 303


def test_unauthenticated_caller_cannot_self_enroll(tmp_path):
    """The sandboxed-agent case: a loopback caller WITHOUT the recovery secret
    must not be able to enroll a password and let itself in."""
    app = _app(tmp_path)
    c = _client(app)
    r = _enroll(app, c, recovery="not-the-recovery-secret")
    assert r.status_code == 401
    # no verifier was written, and no session was minted
    assert "mm_admin" not in c.cookies
    assert c.get("/v1/facts").status_code == 401
    con = db.connect(app.state.settings.db_path)
    try:
        assert not auth.is_enrolled(con)
    finally:
        con.close()


def test_enroll_requires_matching_confirmation_and_minimum_length(tmp_path):
    app = _app(tmp_path)
    c = _client(app)
    assert _enroll(app, c, password="longenough1", confirm="mismatch1").status_code == 400
    assert _enroll(app, c, password="short", confirm="short").status_code == 400
    # neither attempt enrolled anything
    con = db.connect(app.state.settings.db_path)
    try:
        assert not auth.is_enrolled(con)
    finally:
        con.close()


def test_enroll_is_rejected_once_a_password_exists(tmp_path):
    app = _app(tmp_path)
    _enroll(app, _client(app))
    # a second enroll (even with the right recovery secret) is refused — reset
    # is the path to change a set password, so a stray enroll can't clobber it
    r = _enroll(app, _client(app), password=NEW_PASSWORD)
    assert r.status_code == 409
    # the original password still works
    c = _client(app)
    assert c.post("/login", data={"password": PASSWORD},
                  follow_redirects=False).status_code == 303


# ── login: only the password, never the admin token ─────────────────────────
def test_admin_token_is_not_accepted_at_login(tmp_path):
    app = _app(tmp_path)
    _enroll(app, _client(app))
    c = _client(app)
    r = c.post("/login", data={"password": app.state.admin_token},
               follow_redirects=False)
    assert r.status_code == 401
    assert c.get("/v1/facts").status_code == 401


def test_login_before_enrollment_just_fails(tmp_path):
    app = _app(tmp_path)
    c = _client(app)
    r = c.post("/login", data={"password": "anything-at-all"},
               follow_redirects=False)
    assert r.status_code == 401
    assert c.get("/v1/facts").status_code == 401


# ── persistence across a FRESH app instance on the same data directory ──────
def test_password_survives_a_fresh_app_instance(tmp_path):
    """The whole point: enroll once, and after a "restart" (a brand-new
    create_app over the same data dir) the same password still logs in."""
    app1 = _app(tmp_path)
    _enroll(app1, _client(app1))

    app2 = _app(tmp_path)  # simulates a service restart on the same data
    c = _client(app2)
    assert c.post("/login", data={"password": PASSWORD},
                  follow_redirects=False).status_code == 303
    assert c.get("/v1/facts").status_code == 200


def test_browser_sessions_survive_a_restart(tmp_path):
    """Every deploy is a restart, and every restart used to sign every browser
    out (#131). A cookie minted by one instance works on a fresh instance over
    the same data, on loopback and on a trusted host alike."""
    app1 = _app(tmp_path, trusted_hosts=["my-mac.my-tailnet.ts.net"])
    owner = _client(app1)
    _enroll(app1, owner)
    sid = owner.cookies.get("mm_admin")
    assert sid and owner.get("/v1/facts").status_code == 200

    app2 = _app(tmp_path, trusted_hosts=["my-mac.my-tailnet.ts.net"])
    reused = _client(app2)
    reused.cookies.set("mm_admin", sid)
    assert reused.get("/v1/facts").status_code == 200
    phone = TestClient(app2, base_url="https://my-mac.my-tailnet.ts.net")
    phone.cookies.set("mm_admin", sid)
    assert phone.get("/v1/health").status_code == 200


def test_only_a_hash_of_the_session_is_stored(tmp_path, admin_sessions):
    """A copy of the database, or a backup, holds no usable cookie: only the
    SHA-256 of the id, and that hash is not itself a cookie."""
    app = _app(tmp_path)
    owner = _client(app)
    _enroll(app, owner)
    sid = owner.cookies.get("mm_admin")
    assert list(admin_sessions.stored(app)) == [admin_sessions.hash(sid)]
    raw = b"".join(p.read_bytes() for p in (tmp_path / "data").glob("memory.db*"))
    assert raw and sid.encode() not in raw
    stolen = _client(app)
    stolen.cookies.set("mm_admin", admin_sessions.hash(sid))
    assert stolen.get("/v1/facts").status_code == 401


@pytest.mark.parametrize("forge", [
    lambda sid: sid[:-1] + ("A" if sid[-1] != "A" else "B"),  # one char off
    lambda sid: sid + "x",                                    # extended
    lambda sid: sid[:20],                                     # truncated
    lambda sid: "forged",                                     # unknown
    lambda sid: "x" * 5000,                                   # oversized
])
def test_a_tampered_or_unknown_cookie_is_refused(tmp_path, forge):
    app = _app(tmp_path)
    owner = _client(app)
    _enroll(app, owner)
    bad = _client(app)
    bad.cookies.set("mm_admin", forge(owner.cookies.get("mm_admin")))
    assert bad.get("/v1/facts").status_code == 401
    assert "locked" in bad.get("/").text.lower()
    assert owner.get("/v1/facts").status_code == 200


def test_logout_stays_logged_out_across_a_restart(tmp_path):
    app = _app(tmp_path)
    owner = _client(app)
    _enroll(app, owner)
    sid = owner.cookies.get("mm_admin")
    owner.post("/logout", follow_redirects=False)
    stale = _client(_app(tmp_path))
    stale.cookies.set("mm_admin", sid)
    assert stale.get("/v1/facts").status_code == 401


def test_a_restart_clears_sessions_that_ran_out(tmp_path, admin_sessions):
    app = _app(tmp_path)
    owner = _client(app)
    _enroll(app, owner)
    kept = owner.cookies.get("mm_admin")
    gone = admin_sessions.plant(app, expires_at=1.0)
    _app(tmp_path)
    stored = admin_sessions.stored(app)
    assert admin_sessions.hash(gone) not in stored
    assert admin_sessions.hash(kept) in stored


def test_a_new_login_clears_sessions_that_ran_out(tmp_path, admin_sessions):
    app = _app(tmp_path)
    _enroll(app, _client(app))
    gone = admin_sessions.plant(app, expires_at=1.0)
    _client(app).post("/login", data={"password": PASSWORD},
                      follow_redirects=False)
    assert admin_sessions.hash(gone) not in admin_sessions.stored(app)


# ── recovery-gated reset ────────────────────────────────────────────────────
def test_reset_requires_the_recovery_secret(tmp_path):
    app = _app(tmp_path)
    _enroll(app, _client(app))
    c = _client(app)
    r = c.post("/reset", data={"recovery": "wrong", "password": NEW_PASSWORD,
                               "confirm": NEW_PASSWORD}, follow_redirects=False)
    assert r.status_code == 401
    # unchanged: the original password still works, the new one doesn't
    assert c.post("/login", data={"password": PASSWORD},
                  follow_redirects=False).status_code == 303
    assert c.post("/login", data={"password": NEW_PASSWORD},
                  follow_redirects=False).status_code == 401


def test_reset_with_recovery_replaces_the_password(tmp_path):
    app = _app(tmp_path)
    _enroll(app, _client(app))
    c = _client(app)
    r = c.post("/reset", data={"recovery": app.state.admin_token,
                               "password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
               follow_redirects=False)
    assert r.status_code == 303  # reset also logs you in
    # new password works; old one is gone — and it survives a restart
    app2 = _app(tmp_path)
    fresh = _client(app2)
    assert fresh.post("/login", data={"password": NEW_PASSWORD},
                      follow_redirects=False).status_code == 303
    assert fresh.post("/login", data={"password": PASSWORD},
                      follow_redirects=False).status_code == 401


def test_reset_signs_out_every_other_browser_for_good(tmp_path):
    """A reset is how a stolen cookie is ended. It used to leave every other
    session alive until a restart; with sessions on disk it ends them itself,
    and the next start doesn't bring them back (#131)."""
    app = _app(tmp_path)
    owner = _client(app)
    _enroll(app, owner)
    thief = _client(app)
    thief.cookies.set("mm_admin", owner.cookies.get("mm_admin"))
    fixer = _client(app)
    r = fixer.post("/reset", data={"recovery": app.state.admin_token,
                                   "password": NEW_PASSWORD,
                                   "confirm": NEW_PASSWORD},
                   follow_redirects=False)
    assert r.status_code == 303
    assert owner.get("/v1/facts").status_code == 401
    assert thief.get("/v1/facts").status_code == 401
    assert fixer.get("/v1/facts").status_code == 200
    after = _app(tmp_path)
    stale = _client(after)
    stale.cookies.set("mm_admin", owner.cookies.get("mm_admin"))
    assert stale.get("/v1/facts").status_code == 401
    kept = _client(after)
    kept.cookies.set("mm_admin", fixer.cookies.get("mm_admin"))
    assert kept.get("/v1/facts").status_code == 200


def test_a_wrong_recovery_secret_signs_nobody_out(tmp_path):
    app = _app(tmp_path)
    owner = _client(app)
    _enroll(app, owner)
    _client(app).post("/reset", data={"recovery": "wrong",
                                      "password": NEW_PASSWORD,
                                      "confirm": NEW_PASSWORD},
                      follow_redirects=False)
    assert owner.get("/v1/facts").status_code == 200


# ── no secret leakage anywhere an unauthenticated caller can reach ──────────
def test_no_recovery_secret_or_verifier_leaks_to_unauthenticated_callers(tmp_path):
    app = _app(tmp_path, auth_token="a-very-specific-recovery-value")
    # pre-enroll: the enrollment page must not carry the recovery secret
    pre = _client(app).get("/").text
    assert "a-very-specific-recovery-value" not in pre

    _enroll(app, _client(app))
    # the stored scrypt verifier (durable) must never appear in HTTP either
    con = db.connect(app.state.settings.db_path)
    try:
        verifier = db.get_setting(con, auth.SETTING_KEY, "")
    finally:
        con.close()
    assert verifier  # it IS stored...

    c = _client(app)
    for text in (c.get("/").text,
                 c.post("/login", data={"password": "nope"}).text,
                 c.post("/reset", data={"recovery": "nope", "password": "x",
                                        "confirm": "x"}).text):
        assert "a-very-specific-recovery-value" not in text
        assert verifier not in text
        assert PASSWORD not in text
