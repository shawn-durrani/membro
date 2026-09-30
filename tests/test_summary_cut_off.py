"""A profile the model didn't finish is never saved (#163).

The profile writer thinks before it writes, and the thinking comes out of
the same token cap as the profile. When the cap runs out, the reply stops
mid-sentence and loses its last sections, the most current ones. So every
build call checks how its reply ended: a cut-off draft keeps the previous
profile, and a cut-off expand or squeeze keeps the draft it started from.
Each one leaves a log line that says why, with no profile text in it.

Every request here goes through the real utility_complete into a stand-in
client, so what is checked is the stop reason the API would send back.
"""

import logging

import pytest

from memory_service import ledger, llm, summary
from memory_service.config import Settings

FINISHED = "## Identity\n- Alex builds furniture in a home workshop."
# Stands for the words of a reply cut off mid-sentence. None of it may reach
# a log line or an error message.
CUT = "## Identity\n- Alex restores a cedar canoe and"


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Anthropic:
    """Records every messages.create call and answers from a queue of
    (text, stop_reason) pairs."""

    def __init__(self):
        self.requests = []
        self.replies = []
        self.messages = self

    def create(self, **req):
        self.requests.append(req)
        text, stop = self.replies.pop(0)

        class _Resp:
            pass
        resp = _Resp()
        resp.content = [_Text(text)]
        resp.stop_reason = stop
        resp.usage = None
        return resp


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    fake = _Anthropic()
    monkeypatch.setattr(llm, "_clients", {"anthropic": fake})
    monkeypatch.setattr(llm, "_tally", {})
    return fake


def _seed(con, settings):
    ledger.add_fact(con, "Alex is learning woodworking.", settings)


def _seed_ample(con, settings, n=24):
    """Enough material to expand into against an 80-word floor."""
    for i in range(n):
        ledger.add_fact(
            con, f"Alex finished workbench project number {i} at Initech on "
                 f"day {i} of the build, with {i * 3} boards cut and planed.",
            settings)


def _cut_off_lines(caplog):
    return [r for r in caplog.records if r.name == "memory_service.summary"
            and "stopped before it finished" in r.getMessage()]


# ---------------------------------------------------------------- the draft

def test_a_cut_off_draft_keeps_the_previous_profile(con, settings, api,
                                                    caplog):
    _seed(con, settings)
    api.replies = [(FINISHED, "end_turn")]
    summary.regenerate(con, settings)
    before = summary.get(con)
    assert before["summary"] == FINISHED

    api.replies = [(CUT, "max_tokens")]
    with caplog.at_level(logging.WARNING, logger="memory_service.summary"):
        with pytest.raises(RuntimeError) as err:
            summary.regenerate(con, settings)

    assert summary.get(con) == before          # same text, same build time
    assert len(summary.versions(con)) == 1     # no version for the cut-off
    lines = _cut_off_lines(caplog)
    assert len(lines) == 1
    line = lines[0].getMessage()
    assert lines[0].levelno == logging.WARNING
    assert "the draft stopped before it finished" in line
    assert "stop reason max_tokens" in line
    assert f"cap 16000 tokens on {settings.summary_model}" in line
    assert "the previous profile stays" in line
    # The job's error says the same, and neither carries the reply's words.
    assert "stop reason max_tokens" in str(err.value)
    assert "the previous profile stays" in str(err.value)
    for said in (line, str(err.value), caplog.text):
        assert "canoe" not in said


def test_a_cut_off_first_build_saves_nothing(con, settings, api):
    """With no profile yet, a cut-off draft leaves none, rather than one
    that stops mid-sentence."""
    _seed(con, settings)
    api.replies = [(CUT, "max_tokens")]
    with pytest.raises(RuntimeError):
        summary.regenerate(con, settings)
    assert not summary.get(con)["summary"]
    assert summary.versions(con) == []


@pytest.mark.parametrize("stop", ["refusal", "model_context_window_exceeded",
                                  "pause_turn", "tool_use"])
def test_any_other_unnatural_end_is_not_saved(con, settings, api, stop):
    _seed(con, settings)
    api.replies = [(CUT, stop)]
    with pytest.raises(RuntimeError, match=f"stop reason {stop}"):
        summary.regenerate(con, settings)
    assert summary.versions(con) == []


@pytest.mark.parametrize("stop", ["end_turn", "stop_sequence"])
def test_a_finished_draft_still_saves(con, settings, api, caplog, stop):
    _seed(con, settings)
    api.replies = [(FINISHED, stop)]
    with caplog.at_level(logging.WARNING, logger="memory_service.summary"):
        assert summary.regenerate(con, settings) == FINISHED
    assert summary.get(con)["summary"] == FINISHED
    assert len(summary.versions(con)) == 1
    assert _cut_off_lines(caplog) == []


# ---------------------------------------------------------------- the rewrites

def test_a_cut_off_expansion_keeps_the_draft(con, settings, api, caplog):
    """Longer than the draft isn't enough: an expansion that ran out of room
    lost its last sections, however many words it got to first."""
    _seed_ample(con, settings)
    s = settings.model_copy(update={"memory_summary_words": 100,
                                    "memory_summary_fill": 0.8})
    short = "## Identity\n- brief"
    longer_but_cut = "## Identity\n" + "canoe " * 60
    api.replies = [(short, "end_turn"), (longer_but_cut, "max_tokens")]
    with caplog.at_level(logging.WARNING, logger="memory_service.summary"):
        summary.regenerate(con, s)
    assert len(api.requests) == 2
    assert summary.get(con)["summary"] == short
    assert summary.versions(con)[0]["passes"] == []
    lines = [r.getMessage() for r in _cut_off_lines(caplog)]
    assert len(lines) == 1
    assert "the expansion pass stopped" in lines[0]
    assert "the draft stays" in lines[0]
    assert "canoe" not in caplog.text


def test_a_cut_off_squeeze_keeps_the_complete_draft(con, settings, api,
                                                   caplog):
    """A squeeze that ran out of room comes back shorter because it lost its
    last sections. The long but complete draft is the better profile."""
    _seed(con, settings)
    s = settings.model_copy(update={"memory_summary_words": 10,
                                    "memory_summary_fill": 0})
    long_draft = "## Identity\n" + " ".join(["word"] * 50)
    api.replies = [(long_draft, "end_turn"), (CUT, "max_tokens")]
    with caplog.at_level(logging.WARNING, logger="memory_service.summary"):
        summary.regenerate(con, s)
    assert len(api.requests) == 2
    assert summary.get(con)["summary"] == long_draft
    assert summary.versions(con)[0]["passes"] == []
    lines = [r.getMessage() for r in _cut_off_lines(caplog)]
    assert len(lines) == 1
    assert "the squeeze pass stopped" in lines[0]
    assert "the complete draft stays" in lines[0]
    assert "canoe" not in caplog.text


# ---------------------------------------------------------------- the room

def test_the_writer_gets_room_to_think_and_finish(con, settings, api):
    """16,000 tokens at the default budget, and no thinking setting of our
    own: Claude Sonnet 5 refuses a fixed thinking budget, so each model
    thinks the way it does by default and the cap leaves room for both."""
    _seed(con, settings)
    api.replies = [(FINISHED, "end_turn")]
    summary.regenerate(con, settings)
    req = api.requests[0]
    assert req["max_tokens"] == 16000
    assert "thinking" not in req
    # Under the Anthropic SDK's ceiling for a call that doesn't stream: it
    # refuses one it expects to take over ten minutes at 128,000 an hour.
    assert req["max_tokens"] <= 128_000 * 10 / 60


def test_a_bigger_budget_gets_more_room(con, settings, api):
    _seed(con, settings)
    s = settings.model_copy(update={"memory_summary_words": 5000})
    api.replies = [(FINISHED, "end_turn")]
    summary.regenerate(con, s)
    assert api.requests[0]["max_tokens"] == 20000


# ---------------------------------------------------------------- the llm layer

def test_a_cut_off_reply_raises_with_the_reason_and_no_text(settings, api):
    api.replies = [(CUT, "max_tokens")]
    with pytest.raises(llm.CutOffError) as err:
        llm.utility_complete("hi", settings, max_tokens=500,
                             model="claude-sonnet-5", thinking="default")
    e = err.value
    assert (e.model, e.stop_reason, e.max_tokens) == (
        "claude-sonnet-5", "max_tokens", 500)
    assert "canoe" not in str(e)


class _OpenAI:
    finish = "stop"

    def __init__(self, **kwargs):
        self.chat = self
        self.completions = self

    def create(self, **req):
        finish = _OpenAI.finish

        class _Msg:
            content = CUT

        class _Choice:
            message = _Msg()
            finish_reason = finish

        class _Resp:
            choices = [_Choice()]
            usage = None
        return _Resp()


@pytest.mark.parametrize("finish,cut", [("length", True),
                                        ("content_filter", True),
                                        ("stop", False),
                                        (None, False)])
def test_the_compatible_branch_reads_finish_reason(tmp_path, monkeypatch,
                                                   finish, cut):
    """"length" is OpenAI's word for out of room. A server that reports no
    reason can't be checked, so its reply stands."""
    monkeypatch.setattr(llm, "_clients", {})
    monkeypatch.setattr(llm, "OpenAI", _OpenAI)
    monkeypatch.setattr(_OpenAI, "finish", finish)
    s = Settings(data_dir=tmp_path / "d", llm_base_url="http://127.0.0.1:1/v1")
    if cut:
        with pytest.raises(llm.CutOffError, match=f"stop reason {finish}"):
            llm.utility_complete("hi", s, model="qwen3")
    else:
        assert llm.utility_complete("hi", s, model="qwen3") == CUT
