"""Browser sign-ins for the admin page, kept in the database (#131).

A sign-in is an opaque random id in the `mm_admin` cookie. The `sessions`
table holds only the SHA-256 of that id and when it expires, so a restart or
a deploy signs nobody out, and a copy of the database or a backup can't sign
anyone in. Plain SHA-256 is enough because the id is 256 random bits: there
is nothing to guess, so a salt or a slow hash would add cost and no safety,
and a key would have to live on the same disk. Looking up by hash also keeps
the comparison off the secret.

Sign-out ends one sign-in. A password reset, a passkey removal and a
snapshot restore end every one. Expired rows go at startup, on every
sign-in, and when one is presented. The owner decided this for crossband on
27 September 2026 (crossband#471), and membro follows so the fleet behaves
alike.

Only mechanism lives here. Which routes mint or revoke, and who may reach
them, is api.py's business. Every function takes an open connection and
commits its own write.
"""

import hashlib
import secrets
import time

# Far longer than any id this module mints (43 characters), so a giant
# cookie is refused before it is hashed or looked up.
_MAX_SID_LEN = 256


def _sid_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def _usable(sid) -> bool:
    return isinstance(sid, str) and 0 < len(sid) <= _MAX_SID_LEN


def prune(con, now: float | None = None) -> int:
    """Delete every expired sign-in."""
    cur = con.execute("DELETE FROM sessions WHERE expires_at < ?",
                      (time.time() if now is None else now,))
    con.commit()
    return cur.rowcount


def mint(con, ttl_s: float) -> str:
    """A fresh random id, never derived from anything the client sent (no
    fixation). Only its hash is stored."""
    sid = secrets.token_urlsafe(32)
    now = time.time()
    con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    con.execute("INSERT INTO sessions(sid_hash, created_at, expires_at) "
                "VALUES (?, ?, ?)", (_sid_hash(sid), now, now + ttl_s))
    con.commit()
    return sid


def expires_at(con, sid) -> float | None:
    """When this sign-in runs out, or None for an id that was never minted
    or has been revoked."""
    if not _usable(sid):
        return None
    row = con.execute("SELECT expires_at FROM sessions WHERE sid_hash = ?",
                      (_sid_hash(sid),)).fetchone()
    return None if row is None else row[0]


def ok(con, sid) -> bool:
    """True only for a live sign-in. An expired one is deleted on sight."""
    exp = expires_at(con, sid)
    if exp is None:
        return False
    if exp < time.time():
        revoke(con, sid)
        return False
    return True


def revoke(con, sid) -> None:
    if not _usable(sid):
        return
    con.execute("DELETE FROM sessions WHERE sid_hash = ?", (_sid_hash(sid),))
    con.commit()


def revoke_all(con) -> None:
    con.execute("DELETE FROM sessions")
    con.commit()
