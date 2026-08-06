"""Owner password — a durable, memory-hard verifier for the admin UI (#51 slice 1).

Before this, the browser "login" accepted the process's admin token (the value
printed to the terminal at startup, or `MEMORY_AUTH_TOKEN`). That token is
ephemeral by default (a fresh one every restart) and doubles as the MCP/curl
bearer credential — so ordinary browser login demanded terminal access after
every restart, and the everyday login secret was the same string as the machine
credential. This module gives the owner a PASSWORD instead: enrolled once,
durable across restarts, and never equal to the bearer/recovery secret.

What lives here is only the crypto and its storage shape — never a policy
decision. The gate that decides WHO may enroll or reset (the recovery-secret
proof) lives in the HTTP layer; this module just turns a password into a
verifier and checks one against it.

The verifier is `hashlib.scrypt` — a memory-hard, standard-library KDF, so a
stolen verifier can't be brute-forced cheaply and we pull in no new dependency.
We persist ONLY the algorithm parameters, a random per-password salt, and the
derived hash — never the password, and never anything reversible. It is stored
as one JSON row in the existing durable `settings` key/value table (no schema
change, so an existing database simply has no verifier yet and falls into
first-run enrollment). The salt and parameters travel WITH the hash so we can
verify — and, later, transparently upgrade cost — without a migration.

What app-level crypto CANNOT do: defend against a hostile process running as the
same OS user with unrestricted filesystem access. Such a process can read the
verifier row directly. scrypt raises the cost of an OFFLINE guess against a
copied verifier; it is not a substitute for OS-level isolation. See SECURITY.md.
"""

import hashlib
import hmac
import json
import secrets

from . import db

# The durable settings key holding the owner-password verifier JSON. Absent =
# not yet enrolled (a fresh install, or one that predates #51 slice 1).
SETTING_KEY = "owner_password_verifier"

# scrypt cost parameters. n MUST be a power of two; memory use is ~128*r*n
# bytes (here ~16 MiB), enough to make large-scale offline guessing expensive
# on commodity hardware while staying instant for a single interactive login.
# Stored alongside each hash so these can be raised later without a migration.
_N = 2 ** 14          # 16384
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16
# Explicit ceiling so the derivation never trips OpenSSL's default 32 MiB cap
# as parameters grow; comfortably above the ~16 MiB this configuration needs.
_MAXMEM = 64 * 1024 * 1024

# A minimum length is the one password-quality rule we enforce server-side: it
# blocks trivially empty/one-character secrets without pretending to be a full
# strength policy (which would be theatre on a single-owner local service).
MIN_PASSWORD_LEN = 8


def hash_password(password: str) -> str:
    """Derive a fresh salted scrypt verifier for `password`; return the JSON row
    to persist. A new random salt every call means the same password enrolled
    twice yields two different verifiers (no cross-account correlation, no
    rainbow-table reuse)."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
                        dklen=_DKLEN, maxmem=_MAXMEM)
    return json.dumps({
        "alg": "scrypt", "n": _N, "r": _R, "p": _P, "dklen": _DKLEN,
        "salt": salt.hex(), "hash": dk.hex(),
    })


def verify_password(password: str, stored: str) -> bool:
    """True iff `password` matches the persisted verifier `stored`. Recomputes
    the hash with the STORED salt/parameters (so old verifiers keep verifying
    after a cost bump) and compares in constant time. Any malformed or empty
    verifier — including the not-yet-enrolled case — is a clean False, never an
    exception the caller must handle."""
    if not stored:
        return False
    try:
        rec = json.loads(stored)
        if rec.get("alg") != "scrypt":
            return False
        salt = bytes.fromhex(rec["salt"])
        expected = bytes.fromhex(rec["hash"])
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=rec["n"],
                            r=rec["r"], p=rec["p"], dklen=rec["dklen"],
                            maxmem=_MAXMEM)
    except (ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# ---- durable storage (the settings key/value table) ----

def is_enrolled(con) -> bool:
    """Has an owner password ever been set on this database? Drives the choice
    between the first-run enrollment form and the ordinary login form."""
    return bool(db.get_setting(con, SETTING_KEY, ""))


def is_enrolled_path(db_path) -> bool:
    """`is_enrolled` for callers holding a path, not a connection (the startup
    banner). Best-effort: any read failure reads as "not enrolled" so the
    banner degrades to first-run guidance rather than crashing startup."""
    try:
        c = db.connect(db_path)
        try:
            return is_enrolled(c)
        finally:
            c.close()
    except Exception:
        return False


def set_owner_password(con, password: str) -> None:
    """Persist (or replace) the owner-password verifier. Overwriting is how a
    reset works — the previous verifier is not something to keep, and this is
    operational auth state, not the append-only fact ledger those invariants
    protect."""
    db.set_setting(con, SETTING_KEY, hash_password(password))
    con.commit()


def check_owner_password(con, password: str) -> bool:
    """True iff `password` matches the enrolled owner password."""
    return verify_password(password, db.get_setting(con, SETTING_KEY, ""))
