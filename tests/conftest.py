import hashlib
import os

import pytest

from memory_service import db as db_mod
from memory_service import sessions
from memory_service.config import Settings


@pytest.fixture(autouse=True)
def _umask_restored():
    """db.init makes the process umask owner-only (0o077, #133). Put the
    runner's own back after each test, so one test's startup never decides
    the modes another test's files get."""
    old = os.umask(0o022)
    os.umask(old)
    yield
    os.umask(old)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", user_name="Alex",
                    trusted_apps=["multi-model-chat"],
                    grounding_allowlist=["acme"])


@pytest.fixture
def con(settings):
    db_mod.init(settings)
    c = db_mod.connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def fake_llm(monkeypatch):
    """Route utility_complete to a canned response; no network in tests.
    `queue` serves multi-call flows (e.g. draft → rewrite) one reply at a
    time; `fail_when_empty` makes the call after the queue raise instead.
    `prompts` holds each call as one string, system text first, the way the
    compatible branch sends it; `requests` holds the parts as passed."""
    from memory_service import llm
    state = {"response": "NONE", "prompts": [], "models": [], "queue": [],
             "fail_when_empty": False, "requests": []}

    def _fake(prompt, settings, max_tokens=1000, model=None, **kw):
        state["prompts"].append(llm.prompt_text(prompt, kw.get("system")))
        state["requests"].append({"prompt": prompt, "system": kw.get("system"),
                                  "site": kw.get("site")})
        state["models"].append(model)
        state.setdefault("kwargs", []).append(kw)
        if state["queue"]:
            return state["queue"].pop(0)
        if state["fail_when_empty"]:
            raise RuntimeError("no queued response left")
        return state["response"]

    monkeypatch.setattr("memory_service.llm.utility_complete", _fake)
    return state


@pytest.fixture
def sample_conversation(con):
    from memory_service import episodic
    msgs = [
        {"external_id": "m1", "speaker": "user",
         "content": "I just moved to Springfield and started at Initech as a data engineer.",
         "created_at": 1700000000.0},
        {"external_id": "m2", "speaker": "claude",
         "content": "Congratulations on the Initech role!", "created_at": 1700000060.0},
    ]
    episodic.ingest(con, "multi-model-chat", "chat-1", msgs, title="Weekend plans")
    return msgs


class _AdminSessions:
    """Admin page sign-ins live in an app's database (#131). Tests that need
    one without a login, or need to move its expiry, go through here."""

    @staticmethod
    def hash(sid):
        return hashlib.sha256(sid.encode()).hexdigest()

    def plant(self, app, expires_at=1e12):
        c = db_mod.connect(app.state.settings.db_path)
        try:
            sid = sessions.mint(c, 60)
        finally:
            c.close()
        self.set_expiry(app, sid, expires_at)
        return sid

    def set_expiry(self, app, sid, expires_at):
        c = db_mod.connect(app.state.settings.db_path)
        try:
            c.execute("UPDATE sessions SET expires_at = ? WHERE sid_hash = ?",
                      (expires_at, self.hash(sid)))
            c.commit()
        finally:
            c.close()

    def stored(self, app):
        """sid hash -> expiry for every row."""
        c = db_mod.connect(app.state.settings.db_path)
        try:
            return {r[0]: r[1] for r in
                    c.execute("SELECT sid_hash, expires_at FROM sessions")}
        finally:
            c.close()


@pytest.fixture
def admin_sessions():
    return _AdminSessions()
