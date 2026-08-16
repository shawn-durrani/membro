"""#59: routing and key rules for the utility-model layer."""

import pytest

from memory_service import llm
from memory_service.config import Settings


@pytest.fixture(autouse=True)
def fresh_clients(monkeypatch):
    monkeypatch.setattr(llm, "_clients", {})


def _settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path / "d", **kw)


class _FakeOpenAI:
    """Captures constructor kwargs and the last request; returns 'ok'."""
    built: list = []
    last_request: dict = {}

    def __init__(self, **kwargs):
        _FakeOpenAI.built.append(kwargs)

        class _Completions:
            @staticmethod
            def create(**req):
                _FakeOpenAI.last_request = req

                class _Msg:
                    content = "ok"

                class _Choice:
                    message = _Msg()

                class _Resp:
                    choices = [_Choice()]

                return _Resp()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def test_claude_model_still_demands_anthropic_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = _settings(tmp_path, llm_base_url="http://127.0.0.1:11434/v1")
    with pytest.raises(llm.MissingKeyError):
        llm.utility_complete("hi", s, model="claude-haiku-4-5")


def test_compat_model_without_base_url_demands_openai_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(llm.MissingKeyError):
        llm.utility_complete("hi", _settings(tmp_path), model="qwen3")


def test_base_url_needs_no_key_and_gets_placeholder(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm, "OpenAI", _FakeOpenAI)
    _FakeOpenAI.built = []
    s = _settings(tmp_path, llm_base_url="http://127.0.0.1:11434/v1")
    assert llm.utility_complete("hi", s, model="qwen3") == "ok"
    assert _FakeOpenAI.built == [
        {"base_url": "http://127.0.0.1:11434/v1", "api_key": "local"}]


def test_real_key_wins_over_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "OpenAI", _FakeOpenAI)
    _FakeOpenAI.built = []
    s = _settings(tmp_path, llm_base_url="http://127.0.0.1:11434/v1")
    llm.utility_complete("hi", s, model="qwen3")
    assert _FakeOpenAI.built == [{"base_url": "http://127.0.0.1:11434/v1"}]


def test_vision_on_compat_branch_builds_data_urls(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm, "OpenAI", _FakeOpenAI)
    s = _settings(tmp_path, llm_base_url="http://127.0.0.1:11434/v1")
    llm.utility_vision("describe", [("image/png", "QUJD")], s, model="llava")
    content = _FakeOpenAI.last_request["messages"][0]["content"]
    assert content[0]["image_url"]["url"] == "data:image/png;base64,QUJD"
    assert content[-1] == {"type": "text", "text": "describe"}
