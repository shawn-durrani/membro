"""Passkey (WebAuthn) login — #27.

The everyday unlock becomes a platform-authenticator passkey; the password
stays as the fallback and the recovery secret keeps its enrolment/reset role.
The acceptance surface here:

- Enrolment is possible ONLY from an already-authenticated caller (session or
  bearer), never the lock screen — the sandboxed-agent rule from #51 again.
- A passkey is bound to the origin it was enrolled on: `localhost` and a
  trusted tailnet host enrol separately, an IP origin can hold nothing, and an
  assertion signed for one origin is refused on another.
- A successful assertion mints the SAME kind of opaque, expiring, revocable
  session as a password login; a failed or replayed one mints nothing.
- Credentials persist across a restart (durable settings table); in-flight
  ceremonies and sessions do not (in-memory by design).

Tests run keyless and offline: the "authenticator" is a tiny software P-256
passkey built on py_webauthn's own dependencies (cryptography, cbor2), signing
exactly the byte layouts a real platform authenticator produces.
"""

import hashlib
import json

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from memory_service import auth, db, passkeys
from memory_service.api import create_app
from memory_service.config import Settings

PASSWORD = "a-durable-owner-passphrase"
LOCAL_ORIGIN = "http://localhost"
TAILNET_HOST = "cat.tailtest.ts.net"
TAILNET_ORIGIN = f"https://{TAILNET_HOST}:8443"


# ── a minimal software platform authenticator ───────────────────────────────

class SoftPasskey:
    """A P-256 passkey that behaves like a platform authenticator for one RP:
    same byte layouts, same signatures, no hardware. `sign_count` stays 0 by
    default, exactly like Apple's authenticators, unless a test drives it."""

    def __init__(self, rp_id="localhost"):
        self.rp_id = rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        import secrets as _s
        self.cred_id = _s.token_bytes(16)

    def _cose_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1,
                            -2: nums.x.to_bytes(32, "big"),
                            -3: nums.y.to_bytes(32, "big")})

    @staticmethod
    def _client_data(kind: str, challenge_b64u: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge_b64u,
                           "origin": origin, "crossOrigin": False}).encode()

    def register(self, public_key_options: dict, origin: str) -> dict:
        cdj = self._client_data("webauthn.create",
                                public_key_options["challenge"], origin)
        flags = 0x01 | 0x04 | 0x40  # UP | UV | AT (attested credential data)
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest()
                     + bytes([flags]) + (0).to_bytes(4, "big")
                     + bytes(16)  # zero AAGUID, like fmt "none"
                     + len(self.cred_id).to_bytes(2, "big")
                     + self.cred_id + self._cose_key())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": bytes_to_base64url(self.cred_id),
                "rawId": bytes_to_base64url(self.cred_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cdj),
                             "attestationObject": bytes_to_base64url(att)}}

    def assertion(self, public_key_options: dict, origin: str, *,
                  sign_count: int = 0) -> dict:
        cdj = self._client_data("webauthn.get",
                                public_key_options["challenge"], origin)
        flags = 0x01 | 0x04  # UP | UV
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest()
                     + bytes([flags]) + sign_count.to_bytes(4, "big"))
        sig = self.key.sign(auth_data + hashlib.sha256(cdj).digest(),
                            ec.ECDSA(hashes.SHA256()))
        return {"id": bytes_to_base64url(self.cred_id),
                "rawId": bytes_to_base64url(self.cred_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cdj),
                             "authenticatorData": bytes_to_base64url(auth_data),
                             "signature": bytes_to_base64url(sig),
                             "userHandle": None}}


# ── plumbing ────────────────────────────────────────────────────────────────

def _app(tmp_path, **kw):
    return create_app(Settings(data_dir=tmp_path / "data", **kw))


def _client(app, base_url=LOCAL_ORIGIN, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, base_url=base_url, headers=headers)


def _owner(app, base_url=LOCAL_ORIGIN):
    """A client holding the bearer token — the enrolled-and-unlocked owner."""
    return _client(app, base_url=base_url, token=app.state.admin_token)


def _enrol_passkey(client, origin=LOCAL_ORIGIN, rp="localhost", pk=None):
    pk = pk or SoftPasskey(rp)
    o = client.post("/webauthn/register/options", headers={"Origin": origin})
    assert o.status_code == 200, o.text
    body = {"cid": o.json()["cid"],
            "credential": pk.register(o.json()["publicKey"], origin)}
    r = client.post("/webauthn/register", json=body, headers={"Origin": origin})
    return pk, r


def _passkey_login(client, pk, origin=LOCAL_ORIGIN, *, sign_count=0,
                   mangle=None):
    o = client.post("/webauthn/login/options", headers={"Origin": origin})
    if o.status_code != 200:
        return o
    cred = pk.assertion(o.json()["publicKey"],
                        origin if mangle is None else mangle,
                        sign_count=sign_count)
    return client.post("/webauthn/login",
                       json={"cid": o.json()["cid"], "credential": cred},
                       headers={"Origin": origin})


# ── policy: where a passkey may exist at all ────────────────────────────────

def test_rp_for_host_policy():
    assert passkeys.rp_for_host("localhost", []) == "localhost"
    assert passkeys.rp_for_host("LOCALHOST", []) == "localhost"
    assert passkeys.rp_for_host("127.0.0.1", []) is None        # IPs never
    assert passkeys.rp_for_host("::1", []) is None
    assert passkeys.rp_for_host(TAILNET_HOST, []) is None       # not trusted
    assert passkeys.rp_for_host(TAILNET_HOST, [TAILNET_HOST]) == TAILNET_HOST
    assert passkeys.rp_for_host("100.101.102.103",
                                ["100.101.102.103"]) is None    # trusted ≠ valid
    assert passkeys.rp_for_host(None, []) is None


def test_origin_policy_https_anywhere_http_only_for_localhost():
    assert passkeys.origin_ok("http://localhost", "localhost")
    assert passkeys.origin_ok("http://localhost:8901", "localhost")
    assert passkeys.origin_ok(TAILNET_ORIGIN, TAILNET_HOST)
    assert not passkeys.origin_ok(f"http://{TAILNET_HOST}", TAILNET_HOST)
    assert not passkeys.origin_ok("http://localhost", TAILNET_HOST)  # mismatch
    assert not passkeys.origin_ok("https://evil.example", "localhost")
    assert not passkeys.origin_ok("", "localhost")


def test_register_options_refused_on_ip_origin(tmp_path):
    app = _app(tmp_path)
    c = _owner(app, base_url="http://127.0.0.1")
    r = c.post("/webauthn/register/options",
               headers={"Origin": "http://127.0.0.1"})
    assert r.status_code == 400
    assert "localhost" in r.json()["error"]["message"]


# ── enrolment is never anonymous ────────────────────────────────────────────

def test_enrolment_requires_an_authenticated_caller(tmp_path):
    app = _app(tmp_path)
    c = _client(app)  # no session, no bearer
    assert c.post("/webauthn/register/options",
                  headers={"Origin": LOCAL_ORIGIN}).status_code == 401
    assert c.post("/webauthn/register", json={"cid": "x" * 24, "credential": {}},
                  headers={"Origin": LOCAL_ORIGIN}).status_code == 401
    assert c.get("/webauthn/credentials").status_code == 401
    assert c.delete("/webauthn/credentials/anything").status_code == 401


# ── the round trip ──────────────────────────────────────────────────────────

def test_enrol_then_unlock_with_passkey(tmp_path):
    app = _app(tmp_path)
    pk, r = _enrol_passkey(_owner(app))
    assert r.status_code == 200 and r.json()["ok"]

    visitor = _client(app)  # a fresh, anonymous browser
    assert visitor.get("/v1/facts").status_code == 401
    login = _passkey_login(visitor, pk)
    assert login.status_code == 200 and login.json()["ok"]
    assert visitor.cookies.get("mm_admin")
    assert visitor.get("/v1/facts").status_code == 200


def test_lock_screen_offers_passkey_only_where_one_exists(tmp_path):
    app = _app(tmp_path)
    # password enrolled so the ordinary login layout renders
    _client(app).post("/enroll", data={"recovery": app.state.admin_token,
                                       "password": PASSWORD,
                                       "confirm": PASSWORD})
    before = _client(app).get("/").text
    assert "Unlock with passkey" not in before

    _enrol_passkey(_owner(app))
    on_localhost = _client(app).get("/").text
    assert "Unlock with passkey" in on_localhost
    assert "Use your password instead" in on_localhost
    # the same install browsed via the IP stays password-first, quietly
    on_ip = _client(app, base_url="http://127.0.0.1").get("/").text
    assert "Unlock with passkey" not in on_ip
    assert 'name="password"' in on_ip


def test_lock_screen_and_options_leak_no_credential_material(tmp_path):
    app = _app(tmp_path)
    pk, _ = _enrol_passkey(_owner(app))
    anon = _client(app)
    page = anon.get("/").text
    options = anon.post("/webauthn/login/options",
                        headers={"Origin": LOCAL_ORIGIN}).json()
    cred_id = bytes_to_base64url(pk.cred_id)
    for surface in (page, json.dumps(options)):
        assert cred_id not in surface
        assert "public_key" not in surface
    # discoverable credentials: no allowCredentials disclosed at all
    assert not options["publicKey"].get("allowCredentials")


def test_password_fallback_still_works_with_passkey_enrolled(tmp_path):
    app = _app(tmp_path)
    _client(app).post("/enroll", data={"recovery": app.state.admin_token,
                                       "password": PASSWORD,
                                       "confirm": PASSWORD})
    _enrol_passkey(_owner(app))
    c = _client(app)
    assert c.post("/login", data={"password": PASSWORD},
                  follow_redirects=False).status_code == 303


# ── what must fail, fails ───────────────────────────────────────────────────

def test_assertion_for_another_origin_is_refused(tmp_path):
    app = _app(tmp_path)
    pk, _ = _enrol_passkey(_owner(app))
    c = _client(app)
    # signed clientDataJSON claims a different origin than the ceremony's
    r = _passkey_login(c, pk, mangle="https://evil.example")
    assert r.status_code == 401
    assert "mm_admin" not in c.cookies
    assert c.get("/v1/facts").status_code == 401


def test_unknown_credential_is_refused(tmp_path):
    app = _app(tmp_path)
    _enrol_passkey(_owner(app))
    stranger = SoftPasskey()  # valid crypto, never enrolled
    r = _passkey_login(_client(app), stranger)
    assert r.status_code == 401


def test_login_before_any_enrolment_has_no_options(tmp_path):
    app = _app(tmp_path)
    r = _client(app).post("/webauthn/login/options",
                          headers={"Origin": LOCAL_ORIGIN})
    assert r.status_code == 400


def test_challenge_is_single_use(tmp_path):
    app = _app(tmp_path)
    pk, _ = _enrol_passkey(_owner(app))
    c = _client(app)
    o = c.post("/webauthn/login/options", headers={"Origin": LOCAL_ORIGIN}).json()
    cred = pk.assertion(o["publicKey"], LOCAL_ORIGIN)
    first = c.post("/webauthn/login", json={"cid": o["cid"], "credential": cred},
                   headers={"Origin": LOCAL_ORIGIN})
    assert first.status_code == 200
    replay = _client(app).post("/webauthn/login",
                               json={"cid": o["cid"], "credential": cred},
                               headers={"Origin": LOCAL_ORIGIN})
    assert replay.status_code == 401  # the challenge died with its first use


def test_sign_count_regression_is_refused(tmp_path):
    """A counter that goes BACKWARDS means a cloned credential. Apple's
    constant-zero counters never trip this (0 -> 0 verifies); a real regression
    does."""
    app = _app(tmp_path)
    pk, _ = _enrol_passkey(_owner(app))
    assert _passkey_login(_client(app), pk, sign_count=5).status_code == 200
    assert _passkey_login(_client(app), pk, sign_count=3).status_code == 401
    assert _passkey_login(_client(app), pk, sign_count=6).status_code == 200


def test_duplicate_enrolment_is_refused(tmp_path):
    app = _app(tmp_path)
    owner = _owner(app)
    pk, first = _enrol_passkey(owner)
    assert first.status_code == 200
    again = _enrol_passkey(owner, pk=pk)[1]
    assert again.status_code == 409


# ── per-origin enrolment: the tailnet host is its own RP ────────────────────

def test_trusted_host_enrols_and_unlocks_separately(tmp_path):
    app = _app(tmp_path, auth_token="tok-for-tailnet-test",
               trusted_hosts=[TAILNET_HOST])
    local_pk, _ = _enrol_passkey(_owner(app))

    remote_owner = _owner(app, base_url=TAILNET_ORIGIN)
    remote_pk, r = _enrol_passkey(remote_owner, origin=TAILNET_ORIGIN,
                                  rp=TAILNET_HOST)
    assert r.status_code == 200

    # each credential unlocks its own origin...
    anon_remote = _client(app, base_url=TAILNET_ORIGIN)
    assert _passkey_login(anon_remote, remote_pk,
                          origin=TAILNET_ORIGIN).status_code == 200
    # ...and is invisible to the other (the localhost key signs the localhost
    # RP hash, which cannot verify against the tailnet RP)
    assert _passkey_login(_client(app, base_url=TAILNET_ORIGIN), local_pk,
                          origin=TAILNET_ORIGIN).status_code == 401


def test_untrusted_host_gets_no_ceremony(tmp_path):
    app = _app(tmp_path, auth_token="tok")
    c = _client(app, base_url="https://stranger.example", token="tok")
    r = c.post("/webauthn/register/options",
               headers={"Origin": "https://stranger.example"})
    assert r.status_code == 400


# ── lifecycle: persistence, restart, removal ────────────────────────────────

def test_credentials_survive_a_restart_ceremonies_do_not(tmp_path):
    app1 = _app(tmp_path)
    pk, _ = _enrol_passkey(_owner(app1))
    # an options challenge minted by instance 1...
    o = _client(app1).post("/webauthn/login/options",
                           headers={"Origin": LOCAL_ORIGIN}).json()

    app2 = _app(tmp_path)  # restart on the same data dir
    # ...is meaningless to instance 2 (in-memory ceremonies)
    cred = pk.assertion(o["publicKey"], LOCAL_ORIGIN)
    stale = _client(app2).post("/webauthn/login",
                               json={"cid": o["cid"], "credential": cred},
                               headers={"Origin": LOCAL_ORIGIN})
    assert stale.status_code == 401
    # but the CREDENTIAL persisted: a fresh ceremony on app2 unlocks
    assert _passkey_login(_client(app2), pk).status_code == 200


def test_removal_stops_unlocking_but_password_remains(tmp_path):
    app = _app(tmp_path)
    _client(app).post("/enroll", data={"recovery": app.state.admin_token,
                                       "password": PASSWORD,
                                       "confirm": PASSWORD})
    owner = _owner(app)
    pk, _ = _enrol_passkey(owner)
    listed = owner.get("/webauthn/credentials").json()["credentials"]
    assert len(listed) == 1 and listed[0]["rp_id"] == "localhost"

    assert owner.delete(
        f"/webauthn/credentials/{listed[0]['id']}").status_code == 200
    assert owner.get("/webauthn/credentials").json()["credentials"] == []
    # no options any more, and the old assertion path is gone with them
    assert _client(app).post("/webauthn/login/options",
                             headers={"Origin": LOCAL_ORIGIN}).status_code == 400
    # the fallback is intact
    c = _client(app)
    assert c.post("/login", data={"password": PASSWORD},
                  follow_redirects=False).status_code == 303


def test_stored_record_is_public_material_only(tmp_path):
    app = _app(tmp_path)
    pk, _ = _enrol_passkey(_owner(app))
    con = db.connect(app.state.settings.db_path)
    try:
        rows = passkeys.list_credentials(con)
    finally:
        con.close()
    assert len(rows) == 1
    rec = rows[0]
    assert rec["rp_id"] == "localhost"
    assert rec["origin"] == LOCAL_ORIGIN
    # the stored public key is the COSE key the authenticator produced —
    # and nothing resembling a private scalar is anywhere in the record
    priv = pk.key.private_numbers().private_value.to_bytes(32, "big")
    assert bytes_to_base64url(priv) not in json.dumps(rec)
    assert base64url_to_bytes(rec["id"]) == pk.cred_id
