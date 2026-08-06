"""HTTP API — the full surface, per docs/API.md (contract v1).

Local-only by default: serving on a non-loopback host without an auth token is
refused at startup. DELETE /facts/{id} is the single hard-delete path in the
entire service, and it only exists here — human-initiated by definition.

Exact-row endpoints (GET /facts, GET /review, and every verb that reads or
writes ONE existing fact by id — PATCH, supersede, approve, dismiss, DELETE)
require the owner admin token ALWAYS, even on loopback (#46 v2/v3) — see
`_admin_auth` below. That is a stricter, separate check from the
DNS-rebinding/non-loopback middleware further down, which still governs the
rest of the surface unchanged.

The admin UI authenticates via a `POST /login` + httpOnly session-cookie flow
(#46 v3), not a token embedded in the page: v2 served the real admin token to
ANY unauthenticated `GET /`, which was the same bypass one hop removed — any
loopback caller could fetch the page, read the token, then call the gated
endpoints. `GET /` now serves the real UI ONLY once the browser already holds
a valid session cookie; otherwise it gets a locked page with nothing to leak.

The session cookie holds an OPAQUE, high-entropy, server-side session id —
NOT the admin bearer token itself (#46 v4). v3 set `mm_admin` to the real
token; `HttpOnly` stopped page JS from reading it, but any same-origin request
still carried a reusable, non-expiring, non-revocable credential — a copied
cookie was forever equivalent to the real token, and "logout" could only ever
clear the browser's own copy, never invalidate one already exfiltrated.
`app.state.admin_sessions` maps session id -> expiry; login mints a fresh
random id server-side, logout pops it (true revocation), and every check
rejects an expired id. The MCP admin server keeps using the real bearer token
directly (`Authorization` header) — untouched by any of this.

OWNER PASSWORD (#51 slice 1): the everyday browser login is now a durable
PASSWORD, not the process's admin token. `POST /login` checks the password
against a memory-hard scrypt verifier (see `auth.py`) persisted in the durable
store, so it survives restarts — no more hunting the terminal for a token after
every restart. The admin token keeps two, and ONLY two, jobs: the MCP/curl
`Authorization: Bearer` credential (unchanged), and the OUT-OF-BAND RECOVERY
SECRET that gates first-run enrollment and password reset. Enrollment/reset
(`POST /enroll`, `POST /reset`) require that recovery secret, so an
unauthenticated caller sharing loopback — a sandboxed coding agent — cannot
self-enroll a password and let itself in: it never sees the terminal/`.env`
secret. Sessions remain opaque, HttpOnly, SameSite=Strict, in-memory (cleared
on restart), expiring, and revocable, exactly as v4 left them.
"""

import datetime
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import access, auth, db, embeddings, episodic, jobs, ledger, mining, recall, summary, viz, walls
from .config import Settings, load_settings


class IngestAttachment(BaseModel):
    filename: str = ""
    mime: str = ""
    # ~25MB decoded (b64 is 4/3 the size). A sanity bound, not a budget —
    # storage is uncapped by design; this only keeps one request reasonable.
    data_b64: str = Field(..., max_length=34_000_000)


class IngestMessage(BaseModel):
    external_id: str
    speaker: str
    content: str
    created_at: str  # ISO 8601
    # additive, contract 1.0 (2026-07-11); per-message count capped so one
    # request can't balloon past what the per-file bound intended (security
    # pass 2026-07-11 — the trust boundary is local, the memory isn't infinite)
    attachments: list[IngestAttachment] = Field([], max_length=20)


class IngestBody(BaseModel):
    source_app: str
    conversation_id: str
    title: str = ""
    messages: list[IngestMessage]


class DistillBody(BaseModel):
    source_app: str
    conversation_id: str
    regenerate_summary: bool = True  # False for bulk runs: regenerate once at the end


class SearchBody(BaseModel):
    query: str
    limit: int = Field(20, ge=1, le=500)


class FactBody(BaseModel):
    content: str
    event_date: str | None = None
    confidence: str = "high"
    origin_agent: str = "user"
    source_app: str | None = None


class FactPatch(BaseModel):
    content: str | None = None
    event_date: str | None = None
    confidence: str | None = None
    importance: int | None = Field(None, ge=1, le=10)  # additive, 2026-07-11


class SupersedeBody(BaseModel):
    successor_id: int


class QuarantineBody(BaseModel):
    # reason is REQUIRED and non-empty: a fact pulled out of canon has to say
    # why, or the review queue is a pile of rows nobody can adjudicate (#72).
    ids: list[int]
    reason: str = Field(min_length=1)


class RecallBody(BaseModel):
    query: str = ""
    # 50, not 500: recall is a retrieval aid, and an unbounded top-N over an
    # empty query is a whole-ledger export primitive (#56). Nothing legitimate
    # asks for hundreds of facts in one reach.
    limit: int = Field(10, ge=1, le=50)
    include_superseded: bool = False
    # access-log label (additive, contract 1.0): "auto" marks ambient recalls a
    # client fires on the user's behalf, vs a model deliberately reaching in
    origin: str = Field("http", pattern=r"^[a-z][a-z0-9:_-]{0,31}$")


# The exact recall projection the contract documents (docs/API.md "POST /recall").
# recall.recall() returns whole ledger rows because internal callers want them;
# the HTTP surface must not, since /recall answers unauthenticated loopback
# callers by design. Add a field here only by amending the contract too (#56).
RECALL_FIELDS = ("id", "content", "event_date", "confidence",
                 "origin_agent", "score")


def _recall_out(f: dict) -> dict:
    return {k: f[k] for k in RECALL_FIELDS if k in f}


def _ts(iso: str | None) -> float | None:
    if not iso or not iso.strip():
        return None
    try:
        return datetime.datetime.fromisoformat(iso.strip()).timestamp()
    except ValueError:
        raise HTTPException(422, f"bad date: {iso!r}")


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# The ONLY paths a browser may reach unauthenticated on a trusted (tailnet)
# host: the lock screen and the flows that mint a session (#83). Everything
# else there needs the bearer token or a live admin session, so no anonymous
# tailnet caller ever reaches fact data. `/enroll` and `/reset` still demand
# the recovery secret in their own bodies — they are listed here because a
# cookie cannot exist before they run, not because they are unguarded.
LOGIN_SURFACE = {"/", "/login", "/logout", "/enroll", "/reset"}


# Benchmark disposability sentinel (#90 slice 3). A THROWAWAY data dir that the
# benchmark harness provisions carries this file, holding a fresh random token
# the harness itself wrote. `GET /v1/disposable-identity` reads it from the data
# dir THIS instance actually serves and echoes the token back, so developer
# tooling can prove — by a per-instance token round-trip, never by port number —
# that the instance it is talking to is bound to that specific throwaway store,
# and not the operator's real ledger. The real data dir has no sentinel, so the
# live instance reports `disposable: false` and returns no token. The endpoint
# NEVER creates the sentinel, and never returns the auth token, an API key, the
# data-directory path, or any ledger content. This name is defined independently
# of `bench_memory.safety.DISPOSABLE_MARKER_NAME` (the harness must not import
# the service, and the service must not import the harness); a test asserts the
# two stay identical — that shared literal IS the contract.
DISPOSABLE_SENTINEL_NAME = ".bench_memory_disposable"

# The token is a short nonce, so the sentinel is a small regular file. The read
# is bounded to this many bytes; a sentinel LARGER than this is rejected outright
# (not truncated), so a same-named huge file is never a valid token AND is never
# slurped whole into a response. This bound and the strict-UTF-8 parse below are
# a single contract shared verbatim with `bench_memory.safety` (a test pins that
# the two halves reach the same verdict on every hostile input).
_SENTINEL_READ_CAP = 4096


def _parse_sentinel_bytes(raw: bytes) -> str | None:
    """Turn the raw sentinel bytes into a token, or ``None`` for "not a valid
    sentinel". The parse is deliberately STRICT so the endpoint and the harness
    can never disagree: bytes longer than ``_SENTINEL_READ_CAP`` (oversized) or
    that are not valid UTF-8 are rejected outright — never truncated, never
    lossily decoded into a garbage token. An empty/whitespace token is ``None``.
    """
    if len(raw) > _SENTINEL_READ_CAP:
        return None
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return token or None


def _read_disposable_token(data_dir) -> str | None:
    """The benchmark disposability token, if this data dir carries a valid,
    non-empty sentinel; otherwise ``None``. Read-only and total: EVERY problem
    means "not disposable" — never an exception, never a write.

    Fails closed against a hostile sentinel. The endpoint is KEYLESS on loopback,
    so a symlinked sentinel would otherwise turn it into a confused-deputy read
    primitive: `.bench_memory_disposable` pointing at `memory.db` or any file the
    process can read would echo that file's bytes back as ``token``. So the read
    NEVER follows a symlink (``O_NOFOLLOW`` on POSIX; an ``lstat`` pre-check on
    platforms without it) and requires a REGULAR file (``fstat`` after open, so a
    directory, FIFO, or device by that name is refused). The open also carries
    ``O_NONBLOCK`` (where available) so that opening a FIFO/device named as the
    sentinel returns immediately instead of blocking the keyless probe until a
    writer connects — the ``fstat`` check then rejects it. Only after that are
    the bounded bytes parsed."""
    sentinel = Path(data_dir) / DISPOSABLE_SENTINEL_NAME
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    try:
        # No O_NOFOLLOW (e.g. Windows) -> best-effort lstat pre-check; there is a
        # tiny TOCTOU window there, but the sanctioned deployment is POSIX, where
        # the open itself refuses the symlink atomically. O_NONBLOCK keeps a FIFO
        # open from hanging before the fstat below can reject it.
        if not nofollow and sentinel.is_symlink():
            return None
        fd = os.open(sentinel, os.O_RDONLY | nofollow | nonblock)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _SENTINEL_READ_CAP + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return _parse_sentinel_bytes(raw)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db.init(settings)
    app = FastAPI(title="Membro (multi-model-memory)", version=settings.contract_version)
    app.state.settings = settings
    app.state.backup_scheduler_stop = db.start_backup_scheduler(settings)
    app.router.on_shutdown.append(app.state.backup_scheduler_stop.set)
    # Warm the integrity verdict at startup (#79): the one place the full
    # quick_check scan runs synchronously, so no live health probe — and
    # therefore no chat round awaiting one — ever pays for it.
    db.integrity_verdict(settings)

    # The owner admin token for EXACT-ROW endpoints (#46 v2): if MEMORY_AUTH_TOKEN
    # is configured, that's the token — a stable value across restarts, which is
    # what a persistently-registered MCP session needs. Otherwise a fresh random
    # token is minted for this process's lifetime. This is the ONLY credential
    # the MCP admin server / curl ever uses (Authorization: Bearer, unchanged by
    # everything below). Restart-to-rotate is deliberate.
    #
    # #51 slice 1 gives it a SECOND job and takes one away: it is no longer the
    # everyday browser-login secret (that's now the durable owner password), but
    # it IS the out-of-band RECOVERY SECRET that gates first-run enrollment and
    # password reset (POST /enroll, POST /reset). Same value, narrower everyday
    # role: proof-of-owner for setting a password, never the password itself.
    app.state.admin_token = settings.auth_token or secrets.token_urlsafe(32)

    # Server-side session store for the BROWSER path (#46 v4): sid -> expiry
    # (unix epoch). The cookie holds only this opaque, random sid — never the
    # admin token — so a copied cookie is a revocable, expiring capability, not
    # a standing credential equivalent to the token itself. In-memory and
    # per-process on purpose: a restart is a fresh, empty store, so every
    # browser session must re-authenticate (matches the token's own restart
    # behavior when unconfigured, and keeps this out of the ledger entirely —
    # no schema, no persistence, nothing for #46's "no ledger mutation" scope
    # to touch).
    app.state.admin_sessions = {}
    # Exposed on app.state (not just a closure local) so tests can inspect/
    # force-expire real sessions without reaching into private closures.
    app.state.admin_session_ttl = 60 * 60 * 24  # 24h — a deliberately bounded default
    ADMIN_SESSION_TTL = app.state.admin_session_ttl

    # The trust boundary, enforced per-request (not just at bind time, so it
    # holds however the app is served). Two things at once:
    # 1. DNS-rebinding defense — a malicious page re-pointing its domain at
    #    127.0.0.1 arrives with Host: evil.com and is refused (CORS can't help;
    #    the browser considers it same-origin).
    # 2. The auth token is REAL — any request whose Host isn't loopback needs
    #    Authorization: Bearer <MEMORY_AUTH_TOKEN>. No token configured means
    #    strictly loopback, no exceptions.
    #
    # 3. TRUSTED HOSTS (#83): a browser cannot attach an Authorization header
    #    to a navigation, so rule 2 alone made the owner's own phone — over
    #    the tailnet, the one sanctioned non-loopback path — unable to reach
    #    even the lock screen, leaving #51's password gate unreachable from
    #    the device it exists for. A host the operator explicitly names in
    #    `trusted_hosts` therefore admits the BROWSER surface, with the
    #    password/session flow as the gate: a live admin session passes, and
    #    otherwise only LOGIN_SURFACE (the lock screen + the credential
    #    flows). Fact data — /v1/recall, /v1/search, /v1/summary, every
    #    ledger route — still needs the token or a session on a trusted host,
    #    exactly as before. `trusted_hosts` is empty by default, so nothing
    #    widens for an operator who does not opt in.
    def _cross_site(request: Request) -> bool:
        """Explicitly cross-site per the browser's own Fetch metadata. A
        malicious page that knows the tailnet name still cannot drive this
        API from its own origin (standard same-origin defence in depth).
        A missing header (curl, older browsers) is not treated as cross-site
        — those callers are governed by the credential rules above."""
        return request.headers.get("sec-fetch-site", "").lower() == "cross-site"

    @app.middleware("http")
    async def _local_or_token(request: Request, call_next):
        host = (request.url.hostname or "").lower()
        if host not in LOCAL_HOSTS:
            tok = settings.auth_token
            auth = request.headers.get("authorization", "")
            has_token = bool(tok) and hmac.compare_digest(auth, f"Bearer {tok}")
            if not has_token:
                trusted = host in {h.lower() for h in settings.trusted_hosts}
                session = trusted and _session_ok(
                    request.cookies.get(ADMIN_COOKIE, ""))
                allowed = trusted and not _cross_site(request) and (
                    session or request.url.path in LOGIN_SURFACE)
                if not allowed:
                    return JSONResponse(status_code=403, content={"error": {
                        "code": "403",
                        "message": "loopback only — set MEMORY_AUTH_TOKEN and send "
                                   "Authorization: Bearer <token> to serve beyond "
                                   "localhost, or name this host in "
                                   "MEMORY_TRUSTED_HOSTS to sign in with your "
                                   "owner password from your own tailnet"}})
        return await call_next(request)

    ADMIN_COOKIE = "mm_admin"

    def _session_ok(sid: str) -> bool:
        """True only for a sid this process itself minted at login, that
        hasn't expired. Lazily evicts expired entries so the store never
        grows unbounded from abandoned sessions."""
        if not sid:
            return False
        exp = app.state.admin_sessions.get(sid)
        if exp is None:
            return False
        if exp < db.now():
            app.state.admin_sessions.pop(sid, None)
            return False
        return True

    def _admin_ok(request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        if hmac.compare_digest(auth, f"Bearer {app.state.admin_token}"):
            return True
        return _session_ok(request.cookies.get(ADMIN_COOKIE, ""))

    def _admin_auth(request: Request):
        """The stricter, ALWAYS-enforced gate for exact-row endpoints (#46 v2):
        list/read raw facts, the review queue, and every verb on one existing
        fact by id (edit, supersede, approve, dismiss, delete). Loopback earns
        NO exception here — that's precisely the bypass this closes.

        Two ways in, NEITHER handed out by an unauthenticated response:
        - `Authorization: Bearer <token>` — the MCP admin server / curl, using
          the real owner-configured/minted token directly. Unaffected by
          anything below.
        - The `mm_admin` session cookie — an OPAQUE, random, server-tracked
          session id (#46 v4), not the token itself, set only by POST /login
          after the owner supplies the token. A copied cookie is therefore a
          bounded, revocable capability (expires; dies instantly on logout),
          never a standing equivalent of the real credential.
        A sandboxed process reaching the API directly, with no token and no
        valid session, is refused regardless of host — including for `GET /`
        itself, which hands out neither (#46 v3: embedding the token in the
        page for every caller was the same bypass one hop removed)."""
        if not _admin_ok(request):
            raise HTTPException(401, "owner token required for exact fact/review "
                                      "rows, even on loopback — send Authorization: "
                                      "Bearer <MEMORY_AUTH_TOKEN>, or log in at / "
                                      "(docs/API.md)")

    def con():
        return db.connect(settings.db_path)

    # ---- admin UI ----

    static_dir = Path(__file__).resolve().parent / "static"

    def _enrolled() -> bool:
        # Has the owner ever set a password on this database? Decides which
        # form the locked page shows (first-run enrollment vs ordinary login)
        # and whether /login has anything to check against.
        c = con()
        try:
            return auth.is_enrolled(c)
        finally:
            c.close()

    def _locked_page(error: str = "", *, enrolled: bool | None = None) -> str:
        # Deliberately minimal and dependency-free: no token, no verifier, no
        # ledger data, nothing but a form. Shown to any caller that hasn't
        # authenticated — including a sandboxed process making a bare GET / —
        # so there is NOTHING here for #46 v3's bypass to read. The recovery
        # secret and the password verifier NEVER appear in this HTML (#51).
        if enrolled is None:
            enrolled = _enrolled()
        msg = f'<p class="err">{error}</p>' if error else ""
        head = """<!doctype html><html><head><meta charset="utf-8">
<title>Membro — locked</title>
<style>body{font-family:system-ui,sans-serif;max-width:38em;margin:3em auto;
padding:0 1em;color:#ddd;background:#18181b} .err{color:#f87171}
input{width:100%;padding:.5em;font-size:1em;margin-top:.3em}
button{padding:.5em 1.2em;margin-top:.6em} details{margin-top:1.6em}
label{display:block;margin-top:.6em;font-size:.9em}
small{color:#a1a1aa}</style>
</head><body>
<h1>Membro admin — locked</h1>
<p>This page holds your durable memory — exact facts, review queue, edit/delete.</p>
"""
        if not enrolled:
            # First run (or an install predating #51): no password exists yet.
            # Setting one requires the RECOVERY SECRET — the token printed to
            # the terminal at startup, or your MEMORY_AUTH_TOKEN. That proof
            # stops any other process on this machine from enrolling itself in.
            body = f"""<p><strong>First-time setup.</strong> Choose a password you'll
use to unlock Membro from now on. To prove you're the owner, paste the
<em>recovery secret</em> printed to the terminal when the service started (or
your <code>MEMORY_AUTH_TOKEN</code>). You only do this once.</p>
{msg}
<form method="post" action="/enroll">
<label>Recovery secret<input type="password" name="recovery" autofocus></label>
<label>New password<input type="password" name="password"></label>
<label>Confirm password<input type="password" name="confirm"></label>
<button type="submit">Set password &amp; unlock</button>
</form>
<p><small>The recovery secret is proof of ownership only — it is never your
everyday login and is never stored as your password. A process sharing this
machine that can't see the terminal can't set your password.</small></p>"""
        else:
            # Ordinary case: log in with the enrolled password. A collapsible
            # reset path re-uses the same recovery-secret proof to set a new one.
            body = f"""<p>Enter your password to unlock this browser.</p>
{msg}
<form method="post" action="/login">
<label>Password<input type="password" name="password" autofocus></label>
<button type="submit">Unlock</button>
</form>
<details><summary>Forgot your password?</summary>
<p><small>Reset it with the <em>recovery secret</em> — the token printed to the
terminal at startup, or your <code>MEMORY_AUTH_TOKEN</code>.</small></p>
<form method="post" action="/reset">
<label>Recovery secret<input type="password" name="recovery"></label>
<label>New password<input type="password" name="password"></label>
<label>Confirm password<input type="password" name="confirm"></label>
<button type="submit">Reset password &amp; unlock</button>
</form>
</details>
<p><small>This unlocks only THIS browser, via a private, expiring session.
"Log out" revokes that session everywhere instantly.</small></p>"""
        return head + body + "\n</body></html>"

    def _mint_session_redirect():
        # Mint a FRESH random session id (never derived from any client-supplied
        # value — closes session fixation), record its expiry server-side, set
        # ONLY that opaque id as an httpOnly, SameSite=Strict cookie (#46 v4:
        # the cookie is not the bearer token), and redirect to `/` so nothing
        # sensitive lingers in the address bar or history.
        sid = secrets.token_urlsafe(32)
        app.state.admin_sessions[sid] = db.now() + ADMIN_SESSION_TTL
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(ADMIN_COOKIE, sid, httponly=True, samesite="strict",
                        path="/", max_age=ADMIN_SESSION_TTL)
        return resp

    def _set_password(recovery: str, password: str, confirm: str):
        # Shared body of /enroll and /reset: BOTH require the out-of-band
        # recovery secret (the admin token). This is the gate that stops an
        # unauthenticated caller sharing loopback from setting its own password
        # and letting itself in — "gate the write, don't verify the writer."
        # Wrong/missing recovery → 401, no verifier written. On success, persist
        # a durable scrypt verifier and log the browser straight in.
        if not hmac.compare_digest(recovery, app.state.admin_token):
            return HTMLResponse(
                _locked_page("Recovery secret is incorrect."),
                status_code=401)
        if password != confirm:
            return HTMLResponse(
                _locked_page("The two passwords didn't match."),
                status_code=400)
        if len(password) < auth.MIN_PASSWORD_LEN:
            return HTMLResponse(
                _locked_page(f"Password must be at least {auth.MIN_PASSWORD_LEN} "
                             "characters."), status_code=400)
        c = con()
        try:
            auth.set_owner_password(c, password)
        finally:
            c.close()
        return _mint_session_redirect()

    @app.get("/", include_in_schema=False)
    def admin_ui(request: Request):
        # Single-file dark-theme admin page; no build step, no external assets.
        # Served ONLY to an already-authenticated caller — the session cookie
        # (set by POST /login below) for a browser, or a Bearer header for a
        # curl/script owner who already has the token. An unauthenticated
        # GET / gets the locked page above, never the real UI and never any
        # credential. The UI's own fetches then authenticate via that same
        # cookie automatically (same-origin, no JS needed) — nothing is
        # embedded in the page for a bare request to read.
        if not _admin_ok(request):
            return HTMLResponse(_locked_page())
        return FileResponse(static_dir / "index.html", media_type="text/html")

    @app.post("/login", include_in_schema=False)
    def login(password: str = Form(...)):
        # The everyday owner login: the durable PASSWORD, checked against the
        # scrypt verifier (#51). The admin token is deliberately NOT accepted
        # here — it is the recovery secret for enroll/reset, never the everyday
        # login. Before enrollment there is nothing to check against, so login
        # simply fails and the locked page offers the enrollment form. A wrong
        # password → locked page again with no hint beyond "incorrect", no
        # session created.
        c = con()
        try:
            ok = auth.check_owner_password(c, password)
        finally:
            c.close()
        if not ok:
            return HTMLResponse(_locked_page("Incorrect password."),
                                status_code=401)
        return _mint_session_redirect()

    @app.post("/enroll", include_in_schema=False)
    def enroll(recovery: str = Form(...), password: str = Form(...),
               confirm: str = Form("")):
        # First-run enrollment: only when no password exists yet. Once enrolled,
        # changing the password goes through /reset (same recovery proof) — so a
        # stray enroll can't silently clobber a set password.
        if _enrolled():
            return HTMLResponse(
                _locked_page("A password is already set — use reset instead."),
                status_code=409)
        return _set_password(recovery, password, confirm)

    @app.post("/reset", include_in_schema=False)
    def reset(recovery: str = Form(...), password: str = Form(...),
              confirm: str = Form("")):
        # Recovery-gated password reset. Identical proof to enrollment: the
        # owner presents the out-of-band recovery secret, then a new password.
        return _set_password(recovery, password, confirm)

    @app.post("/logout", include_in_schema=False)
    def logout(request: Request):
        # TRUE server-side revocation: the sid is deleted from the store, so
        # this exact instant invalidates every copy of the cookie anywhere —
        # not just the browser that clicked Log out (#46 v4; v3's cookie WAS
        # the token, so "logout" could only ever clear one browser's copy,
        # never revoke a cookie an attacker had already captured).
        sid = request.cookies.get(ADMIN_COOKIE, "")
        app.state.admin_sessions.pop(sid, None)
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(ADMIN_COOKIE, path="/")
        return resp

    @app.get("/math")
    def math_page():
        # The Mathematics page: the machinery of memory, drawn live (own page —
        # the visuals deserve the room; same tokens/themes as the admin).
        return FileResponse(static_dir / "math.html", media_type="text/html")

    @app.exception_handler(HTTPException)
    async def _err(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={
            "error": {"code": str(exc.status_code), "message": exc.detail}})

    # ---- handshake ----

    @app.get("/v1/health")
    def health():
        h = db.health(settings)
        # Aggregate, in-memory drop-by-reason counts (#66) — an operational
        # signal only ("is the builder-process filter firing a lot?"), never a
        # record of what was dropped. Lives in `detail`, which is explicitly
        # documented as non-contractual and may change without a version bump.
        h["dropped_by_reason"] = walls.drop_diagnostics()
        return {
            "status": "ok" if h["integrity"] == "ok" else "degraded",
            "contract_version": settings.contract_version,
            "db": {"facts": h["facts"]["total"], "messages": h["messages"],
                    "size_bytes": h["size_bytes"], "integrity": h["integrity"],
                    "last_backup_at": h["last_backup_at"]},
            "capabilities": {"embeddings": embeddings.available(),
                              "miner_model": settings.miner_model},
            "detail": h,
        }

    @app.get("/v1/disposable-identity")
    def disposable_identity():
        # Benchmark disposability probe (#90 slice 3). Read-only, loopback-safe,
        # and content-free: it reflects ONLY the benchmark token found in the
        # data dir this instance serves — never the auth token, an API key, the
        # data-dir path, or any ledger content — and it never creates the
        # sentinel. A real instance has no sentinel and answers
        # `{"disposable": false}` with no token, so this is safe on the live
        # service. See DISPOSABLE_SENTINEL_NAME above and docs/API.md.
        token = _read_disposable_token(settings.data_dir)
        if token is None:
            return {"disposable": False}
        return {"disposable": True, "token": token}

    # ---- episodic record ----

    @app.post("/v1/ingest")
    def ingest(body: IngestBody):
        if len(body.messages) > 5000:
            raise HTTPException(413, "too many messages in one ingest (max 5000 per call)")
        c = con()
        try:
            msgs = [{"external_id": m.external_id, "speaker": m.speaker,
                     "content": m.content, "created_at": _ts(m.created_at),
                     "attachments": [a.model_dump() for a in m.attachments]}
                    for m in body.messages]
            res = episodic.ingest(c, body.source_app, body.conversation_id,
                                  msgs, body.title, settings=settings)
        finally:
            c.close()
        return {"ingested": res["ingested"], "skipped": res["skipped"],
                "attached": res["attached"]}

    @app.post("/v1/distill", status_code=202)
    def distill(body: DistillBody):
        def _work():
            c = con()
            try:
                return mining.distill(c, settings, body.source_app,
                                      body.conversation_id,
                                      regenerate=body.regenerate_summary)
            finally:
                c.close()
        return {"job_id": jobs.run("distill", _work)}

    @app.post("/v1/search", dependencies=[Depends(_admin_auth)])
    def search(body: SearchBody):
        # Owner-gated (#125): verbatim transcript snippets are a wider
        # projection than /v1/recall, which was deliberately narrowed to
        # RECALL_FIELDS under #56. Recall stays the open chat-loop surface;
        # search is a human/admin surface. The MCP search tool reads the
        # database directly and is unaffected.
        c = con()
        try:
            hits = episodic.search(c, body.query, body.limit)
            if body.query:  # persists; also feeds the /math live view
                access.record(c, "search", body.query, count=len(hits))
        finally:
            c.close()
        return {"hits": hits}

    # ---- ledger ----

    @app.post("/v1/facts")
    def add_fact(body: FactBody):
        c = con()
        try:
            res = ledger.add_fact(
                c, body.content, settings, source="model" if
                body.origin_agent != "user" else "user",
                origin_agent=body.origin_agent, source_app=body.source_app,
                event_date=_ts(body.event_date), confidence=body.confidence)
        except ValueError as e:
            raise HTTPException(422, str(e))
        finally:
            c.close()
        return res

    @app.get("/v1/facts", dependencies=[Depends(_admin_auth)])
    def list_facts(status: str = "valid", q: str | None = None,
                   limit: int = Query(100, ge=1, le=1000)):
        c = con()
        try:
            return {"facts": ledger.list_facts(c, status=status, query=q, limit=limit)}
        finally:
            c.close()

    @app.patch("/v1/facts/{fact_id}", dependencies=[Depends(_admin_auth)])
    def patch_fact(fact_id: int, body: FactPatch):
        c = con()
        try:
            ok = ledger.update_fact(c, fact_id, content=body.content,
                                    event_date=_ts(body.event_date),
                                    confidence=body.confidence,
                                    importance=body.importance,
                                    settings=settings)
            if not ok:
                raise HTTPException(404, "no such fact or nothing to change")
            return ledger.get_fact(c, fact_id)
        finally:
            c.close()

    @app.post("/v1/facts/{fact_id}/supersede", dependencies=[Depends(_admin_auth)])
    def supersede(fact_id: int, body: SupersedeBody):
        c = con()
        try:
            if not ledger.get_fact(c, body.successor_id):
                raise HTTPException(404, "no such successor fact")
            if not ledger.mark_superseded(c, fact_id, body.successor_id):
                raise HTTPException(409, "fact missing or already superseded")
            return ledger.get_fact(c, fact_id)
        finally:
            c.close()

    @app.post("/v1/facts/quarantine", dependencies=[Depends(_admin_auth)])
    def quarantine_facts(body: QuarantineBody):
        # The missing third treatment for a fact already accepted as canon
        # (#72). DELETE is destructive; supersede asserts a replacement fact
        # that a malformed row doesn't have. This pulls the fact out of recall
        # and the summary, keeps it in the ledger, and files it for review with
        # a reason. Reversible with /approve. Unknown or already-quarantined
        # ids are skipped, not errors, so re-running is a no-op.
        c = con()
        try:
            return ledger.quarantine_many(c, body.ids, body.reason)
        finally:
            c.close()

    @app.post("/v1/facts/{fact_id}/approve", dependencies=[Depends(_admin_auth)])
    def approve(fact_id: int):
        c = con()
        try:
            if not ledger.approve(c, fact_id):
                raise HTTPException(404, "no such fact")
            return ledger.get_fact(c, fact_id)
        finally:
            c.close()

    @app.post("/v1/facts/{fact_id}/dismiss", dependencies=[Depends(_admin_auth)])
    def dismiss(fact_id: int):
        c = con()
        try:
            if not ledger.dismiss(c, fact_id):
                raise HTTPException(409, "fact missing or not quarantined")
            return ledger.get_fact(c, fact_id)
        finally:
            c.close()

    @app.post("/v1/review/dismiss-all", dependencies=[Depends(_admin_auth)])
    def dismiss_all_review():
        # Bulk twin of /v1/facts/{id}/dismiss (#66): clears the whole review
        # queue in one call, same non-destructive semantics — every affected
        # fact stays quarantined and in the ledger, just leaves the queue, and
        # any one of them is reversible with /approve. Meant for clearing a
        # backlog made stale by a filtering fix, not for routine triage.
        c = con()
        try:
            return {"dismissed": ledger.dismiss_all(c)}
        finally:
            c.close()

    @app.delete("/v1/facts/{fact_id}", dependencies=[Depends(_admin_auth)])
    def hard_delete(fact_id: int):
        # THE only hard delete in the service — human-initiated via API/UI.
        c = con()
        try:
            cur = c.execute("DELETE FROM facts WHERE id=?", (fact_id,))
            c.commit()
            if not cur.rowcount:
                raise HTTPException(404, "no such fact")
            return {"deleted": fact_id}
        finally:
            c.close()

    # ---- attachments (admin surface): browse, download, and the one eraser ----

    @app.get("/v1/attachments", dependencies=[Depends(_admin_auth)])
    def list_attachments(limit: int = Query(200, ge=1, le=1000)):
        c = con()
        try:
            rows = [dict(r) for r in c.execute(
                "SELECT a.id, a.filename, a.mime, a.size, a.created_at, "
                "a.extracted_text != '' AS searchable, "
                "c.title AS conversation_title, c.external_id AS conversation_id "
                "FROM attachments a JOIN conversations c ON c.id = a.conversation_id "
                "ORDER BY a.id DESC LIMIT ?", (limit,))]
        finally:
            c.close()
        return {"attachments": rows}

    @app.get("/v1/attachments/{att_id}/file", dependencies=[Depends(_admin_auth)])
    def attachment_file(att_id: int, inline: bool = False):
        c = con()
        try:
            row = c.execute("SELECT filename, mime, stored_name FROM attachments "
                            "WHERE id=?", (att_id,)).fetchone()
        finally:
            c.close()
        if not row:
            raise HTTPException(404, "no such attachment")
        path = settings.data_dir / "attachments" / row["stored_name"]
        if not path.exists():
            raise HTTPException(410, "file missing on disk")
        # inline=1 renders in the browser (image previews); default downloads
        return FileResponse(path, media_type=row["mime"] or "application/octet-stream",
                            filename=None if inline else row["filename"])

    @app.get("/v1/attachments/{att_id}/preview", dependencies=[Depends(_admin_auth)])
    def attachment_preview(att_id: int):
        """The file in its context: an excerpt of what's inside, the message it
        arrived with, and the ledger facts mined from that conversation."""
        c = con()
        try:
            row = c.execute(
                "SELECT a.*, c.title AS conversation_title, c.id AS conv_rowid "
                "FROM attachments a JOIN conversations c ON c.id = a.conversation_id "
                "WHERE a.id=?", (att_id,)).fetchone()
            if not row:
                raise HTTPException(404, "no such attachment")
            msg = c.execute(
                "SELECT speaker, content, created_at FROM messages "
                "WHERE conversation_id=? AND external_id=?",
                (row["conv_rowid"], row["message_external_id"])).fetchone()
            facts = [dict(r) for r in c.execute(
                "SELECT id, content, importance FROM facts "
                "WHERE conversation_id=? AND invalidated_at IS NULL "
                "AND quarantined_at IS NULL ORDER BY id DESC LIMIT 8",
                (row["conv_rowid"],))]
        finally:
            c.close()
        mime = row["mime"] or ""
        kind = ("image" if mime.startswith("image/")
                else "text" if row["extracted_text"] else "binary")
        return {
            "id": row["id"], "filename": row["filename"], "kind": kind,
            "text": (row["extracted_text"] or "")[:4000],
            "text_truncated": len(row["extracted_text"] or "") > 4000,
            "conversation_title": row["conversation_title"],
            "message": dict(msg) if msg else None,
            "facts": facts,
        }

    @app.delete("/v1/attachments/{att_id}", dependencies=[Depends(_admin_auth)])
    def hard_delete_attachment(att_id: int):
        # The attachments twin of the facts eraser — human-initiated via the
        # danger zone, the ONLY delete path. Bytes are content-addressed, so
        # the file itself is only unlinked when no other row references it.
        c = con()
        try:
            row = c.execute("SELECT stored_name, extracted_text FROM attachments "
                            "WHERE id=?", (att_id,)).fetchone()
            if not row:
                raise HTTPException(404, "no such attachment")
            # external-content FTS needs an explicit tombstone before the row goes
            c.execute("INSERT INTO attachments_fts(attachments_fts, rowid, "
                      "extracted_text) VALUES('delete', ?, ?)",
                      (att_id, row["extracted_text"]))
            c.execute("DELETE FROM attachments WHERE id=?", (att_id,))
            shared = c.execute("SELECT 1 FROM attachments WHERE stored_name=? LIMIT 1",
                               (row["stored_name"],)).fetchone()
            c.commit()
        finally:
            c.close()
        removed = False
        if not shared:
            (settings.data_dir / "attachments" / row["stored_name"]).unlink(missing_ok=True)
            removed = True
        return {"deleted": att_id, "file_removed": removed}

    @app.get("/v1/review", dependencies=[Depends(_admin_auth)])
    def review():
        c = con()
        try:
            return {"facts": ledger.review_queue(c)}
        finally:
            c.close()

    # ---- recall & summary ----

    @app.post("/v1/recall")
    def do_recall(body: RecallBody):
        c = con()
        try:
            facts = recall.recall(c, settings, body.query, body.limit,
                                  body.include_superseded)
            # every recall leaves a footprint — ids + scores, the raw material
            # of reinforce-on-reuse; also feeds the /math live view
            access.record(c, "recall", body.query, origin=body.origin,
                          facts=facts)
        finally:
            c.close()
        # Return ONLY the fields the contract promises (docs/API.md): the code
        # was handing back SELECT * — content_hash, conversation_id,
        # source_message_id, quarantine_reason and friends — to a caller that
        # needs none of them. /recall is deliberately open (every chat round
        # uses it), so what it projects IS the security boundary (#56).
        return {"facts": [_recall_out(f) for f in facts]}

    @app.get("/v1/summary")
    def get_summary(request: Request):
        # The chat app fetches this once per round — i.e. once per user
        # question — so it's the observable "fast layer consulted" signal for
        # the /math live view. (Admin-page loads also land here; rare noise.)
        c = con()
        try:
            s = summary.get(c)
            access.record(c, "summary")
        finally:
            c.close()
        # additive fields (contract 1.0): the budget is a promise — show it
        # being kept next to the actual, here and in the admin UI
        s["word_count"] = len((s["summary"] or "").split())
        s["word_budget"] = settings.memory_summary_words
        return s

    # Summary versions (admin surface): the profile's own history. Append-only —
    # regenerating never destroys a version, restoring appends a new row.
    @app.get("/v1/summary/versions")
    def summary_versions():
        c = con()
        try:
            cur = json.loads(db.get_setting(c, "summary_sources") or "{}")
            return {"versions": summary.versions(c),
                    "current_generated_at": cur.get("generated_at")}
        finally:
            c.close()

    @app.get("/v1/summary/versions/{version_id}")
    def summary_version(version_id: int):
        c = con()
        try:
            v = summary.get_version(c, version_id)
        finally:
            c.close()
        if not v:
            raise HTTPException(404, "no such summary version")
        return v

    @app.post("/v1/summary/versions/{version_id}/restore")
    def summary_restore(version_id: int):
        c = con()
        try:
            s = summary.restore(c, version_id, settings)
        finally:
            c.close()
        if s is None:
            raise HTTPException(404, "no such summary version")
        return s

    @app.post("/v1/summary/regenerate", status_code=202)
    def regen_summary():
        def _work():
            c = con()
            try:
                return {"summary_chars": len(summary.regenerate(c, settings))}
            finally:
                c.close()
        return {"job_id": jobs.run("summary", _work)}

    # ---- maintenance ----

    @app.post("/v1/consolidate", status_code=202,
              dependencies=[Depends(_admin_auth)])
    def consolidate():
        from . import consolidate as sweep

        def _work():
            c = con()
            try:
                return sweep.run(c, settings)
            finally:
                c.close()
        return {"job_id": jobs.run("consolidate", _work)}

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(_admin_auth)])
    def job_status(job_id: str):
        # Owner-gated (#125): a job result can embed whole fact rows (a
        # consolidate run returns them), so reading jobs is exact-row
        # access. Callers that fire-and-forget /v1/distill are unaffected.
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return job

    @app.post("/v1/backup")
    def do_backup():
        dest = db.backup(settings)
        return {"snapshot": dest.name if dest else None}

    # ---- Mathematics page (geometry, never FACT content) ------------------
    # The test-enforced invariant is "no fact content", not "nothing
    # readable": /v1/viz/recalls below returns the caller's own queries
    # verbatim, ungated, by design (see docs/API.md, "Open on loopback").

    @app.get("/v1/viz/decay")
    def viz_decay():
        c = con()
        try:
            return viz.decay_data(c)
        finally:
            c.close()

    @app.get("/v1/viz/recalls")
    def viz_recalls(after: float = Query(0.0)):
        # Served from the persistent access log — survives restarts and
        # includes lookups made by MCP client processes, not just this one.
        # `query` is the user's own question verbatim (first 200 chars), so
        # this is the one /viz/* route that hands readable text to an
        # unauthenticated loopback caller.
        c = con()
        try:
            return {"events": access.events_since(c, after), "now": db.now()}
        finally:
            c.close()

    @app.post("/v1/viz/recall_trace")
    def viz_recall_trace(body: SearchBody):
        c = con()
        try:
            return viz.recall_trace(c, settings, body.query, min(body.limit, 20))
        finally:
            c.close()

    _viz_job = {"id": None}

    def _projection_pending():
        """None when the PCA projection is cached and ready; else a
        {"status": "computing"} dict — kicking off the background compute job as
        needed. Shared by the landscape + embeddings endpoints so either one can
        trigger (and wait on) the one-time projection build."""
        c = con()
        try:
            if viz.embeddings_cached(c):
                return None
        finally:
            c.close()
        if _viz_job["id"]:
            j = jobs.get(_viz_job["id"])
            if j and j["status"] == "running":
                return {"status": "computing"}
            if j and j["status"] == "failed":
                _viz_job["id"] = None
                raise HTTPException(500, f"projection failed: {j['error']}")

        def _work():
            c2 = con()
            try:
                return {"points": len(viz.compute_embeddings(c2)["points"])}
            finally:
                c2.close()

        _viz_job["id"] = jobs.run("viz-embeddings", _work)
        return {"status": "computing"}

    @app.get("/v1/viz/landscape")
    def viz_landscape():
        # The one always-current 3D scene (#40). Geometry only; self-sufficient —
        # triggers the projection build when cold, then returns the biome data.
        pending = _projection_pending()
        if pending:
            return pending
        c = con()
        try:
            return viz.landscape_data(c)
        finally:
            c.close()

    @app.get("/v1/viz/embeddings")
    def viz_embeddings():
        pending = _projection_pending()
        if pending:
            return pending
        c = con()
        try:
            return viz.attach_live(c, viz.embeddings_cached(c))
        finally:
            c.close()

    return app


def main():
    import uvicorn
    settings = load_settings()
    if settings.host not in ("127.0.0.1", "localhost", "::1") and not settings.auth_token:
        raise SystemExit(
            f"refusing to bind {settings.host} without MEMORY_AUTH_TOKEN — "
            "this service holds personal data and ships no auth for loopback use only")
    app = create_app(settings)
    # The ONLY place the admin token is ever printed: this process's own
    # stdout (the owner's terminal that ran start.sh), never an HTTP response
    # (#46 v3) and never a file. A sandboxed session sharing this machine's
    # filesystem/network has no route to a live terminal's stdout.
    #
    # #51 slice 1: this value is the RECOVERY SECRET, not the everyday login.
    # First run, it enrolls your durable password; after that you log in with
    # the password and only need this again to reset it (or for MCP/curl Bearer).
    kind = "configured MEMORY_AUTH_TOKEN" if settings.auth_token else "generated for this run — changes on restart"
    enrolled = auth.is_enrolled_path(settings.db_path)
    verb = ("log in with your password; use this only to RESET it" if enrolled
            else "open the page and use this to set your password (first-run enrollment)")
    print(f"Membro admin: open http://{settings.host}:{settings.port}/ — {verb}.\n"
          f"Recovery secret ({kind}):\n  {app.state.admin_token}")
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
