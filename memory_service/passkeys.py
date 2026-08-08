"""Passkey (WebAuthn) credentials for the admin UI (#27).

The everyday unlock becomes a passkey: Touch ID on this Mac, Face ID on a
phone, whatever platform authenticator the browser offers. The password
(auth.py) stays as the fallback and the recovery secret keeps its
enrolment/reset role unchanged; a passkey replaces only the password proof
step, never the session machinery.

A passkey is bound to the exact web origin it was created on. `localhost` and
a tailnet hostname share no domain suffix, so one credential cannot serve
both; the store therefore keeps a LIST of credentials, each tagged with the
Relying Party ID (the hostname) and full origin it belongs to, and the owner
enrols each origin they actually use (#27, decided 2026-08-08). An IP address
is not a valid Relying Party ID at all: browsing via 127.0.0.1 keeps the
password-first lock screen, verified live before this was built.

What lives here is storage and the origin/RP policy, never a ceremony: the
challenge lifecycle and the cryptographic verification (py_webauthn) live in
the HTTP layer, mirroring the auth.py split where the module holds the
mechanism and the HTTP layer decides who may invoke it.

Like the password verifier, credentials live in the durable `settings`
key/value table (no schema change; an existing database simply has no
passkeys yet). Only the PUBLIC key is ever stored: the private half lives in
the platform authenticator (Secure Enclave / iCloud Keychain) and never
reaches this process, so a copied store cannot impersonate the owner's
passkey. Replacing or removing a credential is operational auth state, not
ledger content, exactly like a password reset.
"""

import ipaddress
import json
import secrets

from . import db

# Durable settings keys. Absent = no passkey enrolled (fresh install, or one
# that predates #27).
CREDENTIALS_KEY = "webauthn_credentials"
# One stable random user handle for the single owner, minted at first
# enrolment. WebAuthn wants a user id that is NOT personal data; 16 random
# bytes satisfy every authenticator and identify nothing.
USER_HANDLE_KEY = "webauthn_user_handle"


def rp_for_host(host: str | None, trusted_hosts) -> str | None:
    """The WebAuthn Relying Party ID a request on `host` may use, or None if
    passkeys cannot work there. Policy in one place:

    - IP addresses (127.0.0.1, ::1, a bare tailnet IP) are never valid RP IDs;
      the browser itself refuses them with SecurityError.
    - `localhost` is always allowed: a secure context, a valid domain, and the
      sanctioned loopback way to reach the UI in a browser.
    - Any other hostname must be one the operator explicitly named in
      `trusted_hosts` (the existing #83 trust boundary); the RP ID is then the
      hostname itself, so the same credential works whatever port the tailnet
      proxy publishes.
    """
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return None
    try:
        ipaddress.ip_address(h.strip("[]"))
        return None
    except ValueError:
        pass
    if h == "localhost":
        return h
    if h in {str(t).strip().lower() for t in trusted_hosts}:
        return h
    return None


def origin_ok(origin: str | None, host: str | None) -> bool:
    """True iff `origin` (an Origin request header) names this same host with
    an acceptable scheme: https anywhere, plain http only for localhost. The
    ceremony's expected origin is recorded from the page that started it and
    later checked against the browser-signed clientDataJSON, so it must be an
    origin we would trust serving the lock screen in the first place."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit((origin or "").strip())
        o_host = (parts.hostname or "").lower()
    except ValueError:
        return False
    h = (host or "").strip().lower().rstrip(".")
    if not o_host or o_host != h:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and o_host == "localhost"


# ---- durable storage (the settings key/value table) ----

def list_credentials(con) -> list[dict]:
    """Every enrolled passkey, oldest first. Any malformed stored value reads
    as "none enrolled" rather than an exception: the lock screen must render
    whatever state the store is in."""
    raw = db.get_setting(con, CREDENTIALS_KEY, "")
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def credentials_for_rp(con, rp_id: str) -> list[dict]:
    """The credentials usable on one Relying Party ID; drives both the lock
    screen's "offer a passkey here?" decision and assertion lookup."""
    return [r for r in list_credentials(con) if r.get("rp_id") == rp_id]


def add_credential(con, rec: dict) -> None:
    rows = list_credentials(con)
    rows.append(rec)
    db.set_setting(con, CREDENTIALS_KEY, json.dumps(rows))
    con.commit()


def remove_credential(con, cred_id: str) -> bool:
    """Drop one credential by its (base64url) id. True if something was
    removed. The private key on the device is untouched; the credential
    simply stops unlocking this service."""
    rows = list_credentials(con)
    kept = [r for r in rows if r.get("id") != cred_id]
    if len(kept) == len(rows):
        return False
    db.set_setting(con, CREDENTIALS_KEY, json.dumps(kept))
    con.commit()
    return True


def update_sign_count(con, cred_id: str, sign_count: int) -> None:
    """Persist the authenticator's post-assertion signature counter so a
    cloned-credential replay (counter going backwards) is detectable next
    time. Apple platform authenticators report a constant 0; storing it is
    still correct and costs nothing."""
    rows = list_credentials(con)
    for r in rows:
        if r.get("id") == cred_id:
            r["sign_count"] = int(sign_count)
    db.set_setting(con, CREDENTIALS_KEY, json.dumps(rows))
    con.commit()


def user_handle(con) -> bytes:
    """The stable random WebAuthn user handle for the single owner, minted on
    first use and persisted so every credential, on every origin, belongs to
    the same user entry in the authenticator's eyes."""
    stored = db.get_setting(con, USER_HANDLE_KEY, "")
    if stored:
        try:
            return bytes.fromhex(stored)
        except ValueError:
            pass
    handle = secrets.token_bytes(16)
    db.set_setting(con, USER_HANDLE_KEY, handle.hex())
    con.commit()
    return handle
