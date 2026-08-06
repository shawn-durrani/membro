import pytest

from memory_service import db as db_mod
from memory_service.config import Settings


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
    time; `fail_when_empty` makes the call after the queue raise instead."""
    state = {"response": "NONE", "prompts": [], "models": [], "queue": [],
             "fail_when_empty": False}

    def _fake(prompt, settings, max_tokens=1000, model=None):
        state["prompts"].append(prompt)
        state["models"].append(model)
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
