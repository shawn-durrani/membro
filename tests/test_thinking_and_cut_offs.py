"""Every model call is safe on a model that thinks first (#167).

Claude Sonnet 5 and the newer models think before they answer when a
request leaves `thinking` out, and the thinking comes out of the same
max_tokens as the answer. A short job's small cap can run out before a
word of the answer is written, and a reply can also be refused. So the
short jobs turn thinking off where the model allows it, or get room for
it where it can't be turned off, and every call checks how its reply
ended. A reply cut short or refused is never read as an answer.

Every request here goes through the real utility_complete or
utility_vision into a stand-in client, so what's checked is the request
the API would get and the stop reason it would send back.
"""

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

from memory_service import (captions, consolidate, episodic, judge, ledger,
                            llm, mining, summary)

REPO = Path(__file__).resolve().parent.parent
ROOM = llm.THINKING_ROOM
TS = 1740000000.0
CANOE = "I'm restoring a cedar canoe at the lake."
CANOE_FACT = "NEW src=1 importance=5: Alex is restoring a cedar canoe at the lake."
STOPS = ["max_tokens", "refusal"]


class _Block:
    def __init__(self, kind, text=""):
        self.type = kind
        self.text = text
        self.thinking = ""


class _Anthropic:
    """Records every messages.create call and answers from a queue of
    (text, stop_reason) or (text, stop_reason, "thinking") replies. A
    thinking reply starts with an empty thinking block, the way the
    newer models send one back by default."""

    def __init__(self):
        self.requests = []
        self.replies = []
        self.messages = self

    def create(self, **req):
        self.requests.append(req)
        assert self.replies, "a model call the test didn't expect"
        text, stop, *thinks = self.replies.pop(0)

        class _Resp:
            pass
        resp = _Resp()
        resp.content = ([_Block("thinking")] if thinks else []) + (
            [_Block("text", text)] if text else [])
        resp.stop_reason = stop
        resp.usage = None
        return resp


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake = _Anthropic()
    monkeypatch.setattr(llm, "_clients", {"anthropic": fake})
    monkeypatch.setattr(llm, "_tally", {})
    return fake


@pytest.fixture
def s55(settings):
    """The live miner's model since 30 September."""
    return settings.model_copy(update={"miner_model": "claude-sonnet-5-5"})


# ------------------------------------------------------ the request per model

def test_haiku_gets_the_request_it_always_got(settings, api):
    api.replies = [("importance=5", "end_turn")]
    llm.utility_complete("hi", settings, max_tokens=50,
                         model="claude-haiku-4-5")
    assert api.requests == [{"model": "claude-haiku-4-5", "max_tokens": 50,
                             "messages": [{"role": "user", "content": "hi"}]}]


@pytest.mark.parametrize("model, thinking, cap", [
    ("claude-haiku-4-5", None, 50),
    ("claude-haiku-4-5-20251001", None, 50),
    ("claude-sonnet-4-5", None, 50),
    ("claude-sonnet-4-6", None, 50),
    ("claude-opus-4-8", None, 50),
    ("claude-sonnet-5", {"type": "disabled"}, 50),
    ("claude-opus-5", {"type": "disabled"}, 50),
    ("claude-sonnet-5-5", {"type": "between_tools"}, 50),
    ("claude-opus-5-5", None, 50 + ROOM),
    ("claude-fable-5-1", None, 50 + ROOM),
    ("claude-mythos-5", None, 50 + ROOM),
    # A name this code doesn't know yet is taken to be a newer model.
    ("claude-sonnet-7", None, 50 + ROOM),
])
def test_a_short_job_turns_thinking_off_where_it_can(settings, api, model,
                                                     thinking, cap):
    api.replies = [("importance=5", "end_turn")]
    llm.utility_complete("hi", settings, max_tokens=50, model=model)
    req = api.requests[0]
    assert req.get("thinking") == thinking
    assert req["max_tokens"] == cap


@pytest.mark.parametrize("model, thinking, cap", [
    ("claude-haiku-4-5", {"type": "enabled", "budget_tokens": 1024}, 1300),
    ("claude-sonnet-4-6", {"type": "enabled", "budget_tokens": 1024}, 1300),
    ("claude-opus-4-8", {"type": "adaptive"}, 1300 + ROOM),
    ("claude-sonnet-5", {"type": "adaptive"}, 1300 + ROOM),
    ("claude-sonnet-5-5", {"type": "adaptive"}, 1300 + ROOM),
    ("claude-opus-5-5", {"type": "adaptive"}, 1300 + ROOM),
])
def test_a_budget_goes_only_to_a_model_that_takes_one(settings, api, model,
                                                       thinking, cap):
    api.replies = [("TECHNICAL", "end_turn")]
    llm.utility_complete("hi", settings, max_tokens=1300, model=model,
                         thinking_budget=1024)
    assert api.requests[0]["thinking"] == thinking
    assert api.requests[0]["max_tokens"] == cap


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-sonnet-5",
                                   "claude-sonnet-5-5", "claude-opus-5-5"])
def test_the_profile_leaves_thinking_to_the_model(settings, api, model):
    api.replies = [("## Identity", "end_turn")]
    llm.utility_complete("hi", settings, max_tokens=16000, model=model,
                         thinking="default")
    assert "thinking" not in api.requests[0]
    assert api.requests[0]["max_tokens"] == 16000


def test_the_longest_name_wins():
    assert llm.thinking_support("claude-sonnet-5-5") == (
        {"type": "between_tools"}, False)
    assert llm.thinking_support("claude-sonnet-5-20260101") == (
        {"type": "disabled"}, False)
    assert llm.thinking_support("claude-opus-4-6") == (None, True)
    assert llm.thinking_support("claude-opus-4-7") == (None, False)
    assert llm.thinking_support("claude-sonnet-6") == (llm.CANNOT, False)


def test_a_refusal_raises_with_the_reason_and_no_text(settings, api):
    api.replies = [("Alex restores a cedar", "refusal")]
    with pytest.raises(llm.CutOffError) as err:
        llm.utility_complete("hi", settings, max_tokens=50,
                             model="claude-sonnet-5-5")
    assert err.value.stop_reason == "refusal"
    assert "cedar" not in str(err.value)


# ---------------------------------------------------------------- the miner

def _chat(con, turns, conv="chat-c"):
    episodic.ingest(con, "multi-model-chat", conv, [
        {"external_id": f"m{i}", "speaker": speaker, "content": text,
         "created_at": TS + 60 * i}
        for i, (speaker, text) in enumerate(turns, start=1)])
    c = episodic.get_conversation(con, "multi-model-chat", conv)
    return episodic.messages_after(con, c["id"], 0)


def _mine(con, settings, conv="chat-c"):
    return mining.distill(con, settings, "multi-model-chat", conv,
                          regenerate=False)


def _facts(con):
    return (ledger.list_facts(con, status="valid")
            + ledger.list_facts(con, status="quarantined"))


def _mined_upto(con, conv="chat-c"):
    return episodic.get_conversation(con, "multi-model-chat", conv)["mined_upto"]


def _excerpt_turns(req) -> int:
    """How many turns the miner was shown in this request."""
    text = "".join(b["text"] for b in req["messages"][0]["content"])
    return text.split("## Conversation excerpt\n", 1)[1].count("[msg ")


def test_a_cut_off_miner_reply_is_mined_again_in_halves(con, s55, api):
    msgs = _chat(con, [("user", CANOE),
                       ("user", "The varnish goes on next weekend.")])
    api.replies = [
        # The old miner kept both lines, the second one cut mid-sentence.
        (CANOE_FACT + "\nNEW src=2 importance=4: Alex plans to varnish",
         "max_tokens"),
        (CANOE_FACT, "end_turn"),
        ("NEW src=1 importance=4: Alex is varnishing the canoe.", "end_turn"),
    ]
    _mine(con, s55)
    assert [_excerpt_turns(r) for r in api.requests] == [2, 1, 1]
    assert api.requests[0]["thinking"] == {"type": "between_tools"}
    assert api.requests[0]["max_tokens"] == 1000
    assert sorted(f["content"] for f in _facts(con)) == [
        "Alex is restoring a cedar canoe at the lake.",
        "Alex is varnishing the canoe."]
    assert _mined_upto(con) == msgs[-1]["id"]


def test_thinking_that_ate_the_cap_gets_one_roomy_try(con, s55, api):
    msgs = _chat(con, [("user", CANOE)])
    api.replies = [("", "max_tokens", "thinking"), (CANOE_FACT, "end_turn")]
    _mine(con, s55)
    assert [r["max_tokens"] for r in api.requests] == [
        mining.MINER_TOKENS, mining.MINER_ROOMY_TOKENS]
    assert [f["content"] for f in _facts(con)] == [
        "Alex is restoring a cedar canoe at the lake."]
    assert _mined_upto(con) == msgs[-1]["id"]


def test_a_message_still_cut_off_is_left_unmined(con, settings, api, caplog):
    msgs = _chat(con, [("user", CANOE)])
    cut = "NEW src=1 importance=5: Alex is restoring a cedar"
    api.replies = [(cut, "max_tokens"), (cut, "max_tokens")]
    with caplog.at_level(logging.WARNING, logger="memory_service.mining"):
        res = _mine(con, settings)
    # Haiku: the same request as ever, then one bigger try.
    assert ["thinking" in r for r in api.requests] == [False, False]
    assert [r["max_tokens"] for r in api.requests] == [1000, 4000]
    assert res["unmined"] == 1 and _facts(con) == []
    assert _mined_upto(con) == msgs[-1]["id"]
    assert f"message {msgs[0]['id']}" in caplog.text
    assert "cedar" not in caplog.text


def test_a_refused_message_is_left_unmined_and_the_rest_mined(con, s55, api,
                                                             caplog):
    msgs = _chat(con, [("user", "A turn the model won't read."),
                       ("user", CANOE)])
    api.replies = [("", "refusal"), ("", "refusal"),
                   (CANOE_FACT, "end_turn")]
    with caplog.at_level(logging.WARNING, logger="memory_service.mining"):
        res = _mine(con, s55)
    assert [_excerpt_turns(r) for r in api.requests] == [2, 1, 1]
    assert res["unmined"] == 1
    assert [f["content"] for f in _facts(con)] == [
        "Alex is restoring a cedar canoe at the lake."]
    assert _mined_upto(con) == msgs[-1]["id"]
    assert "stop reason refusal" in caplog.text
    assert "won't read" not in caplog.text


@pytest.mark.parametrize("stop", STOPS)
def test_a_cut_off_importance_retry_holds_the_fact(con, s55, api, stop):
    _chat(con, [("user", CANOE)])
    api.replies = [
        ("NEW src=1: Alex is restoring a cedar canoe at the lake.", "end_turn"),
        ("importance=7", stop),
    ]
    _mine(con, s55)
    assert api.requests[1]["max_tokens"] == 50
    assert api.requests[1]["thinking"] == {"type": "between_tools"}
    (fact,) = _facts(con)
    assert fact["importance"] is None
    assert fact["quarantined_at"] is not None
    assert "did not supply it on retry" in fact["quarantine_reason"]


@pytest.mark.parametrize("stop", STOPS)
def test_a_cut_off_src_retry_holds_the_fact(con, s55, api, stop):
    _chat(con, [("user", "My sister Sam is visiting next week."),
                ("guest:Sam", "Hi everyone."),
                ("user", "I've taken up bouldering at the climbing gym.")],
          conv="chat-g")
    api.replies = [
        ("NEW importance=5: Alex has taken up bouldering at the climbing gym.",
         "end_turn"),
        # A whole binding, but from a reply that didn't finish.
        ("FACT 1: src=3", stop),
    ]
    _mine(con, s55, conv="chat-g")
    (fact,) = _facts(con)
    assert fact["source_message_id"] is None
    assert fact["quarantined_at"] is not None
    assert "none supplied on retry" in fact["quarantine_reason"]


# ------------------------------------------------ the sweep, judge, captions

@pytest.mark.parametrize("stop", STOPS)
def test_a_cut_off_pin_list_nominates_nothing(con, s55, api, stop):
    fid = ledger.add_fact(con, "Alex's sister is called Sam.", s55,
                          importance=8)["id"]
    api.replies = [(str(fid), "end_turn")]
    assert len(consolidate._pin_nominations(con, s55)) == 1
    api.replies = [(str(fid), stop)]
    assert consolidate._pin_nominations(con, s55) == []


def _held(con, reason, content, said):
    episodic.ingest(con, "multi-model-chat", "judge-chat", [
        {"external_id": "j1", "speaker": "user", "content": said,
         "created_at": TS}], title="t")
    conv = episodic.get_conversation(con, "multi-model-chat", "judge-chat")
    msg = episodic.messages_after(con, conv["id"], 0)[0]
    cur = con.execute(
        "INSERT INTO facts(content, source, origin_agent, conversation_id,"
        " source_message_id, created_at, event_date, confidence,"
        " content_hash, quarantined_at, quarantine_reason)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (content, "chat", "multi-model-chat", conv["id"], msg["id"],
         TS, TS, "medium", "held-1", TS, reason))
    con.commit()
    return cur.lastrowid


GROUND = "grounding: Zephyrline not in source chat — review before trusting"


@pytest.mark.parametrize("stop", STOPS)
@pytest.mark.parametrize("reason, reply", [
    (GROUND, '{"Zephyrline": "toured the zephyrline factory"}'),
    (judge.PERSONA_REASON, "TECHNICAL"),
])
def test_a_cut_off_verdict_leaves_the_row_held(con, s55, api, reason, reply,
                                               stop):
    s55.judge_pass = True
    fid = _held(con, reason, "Alex toured the Zephyrline factory.",
                "I toured the zephyrline factory today")
    api.replies = [(reply, stop)]
    out = judge.run_pass(con, s55)
    assert out["cleared"] == 0 and out["relabelled"] == 0
    row = con.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
    assert row["quarantined_at"] is not None
    assert row["quarantine_reason"] == reason


@pytest.mark.parametrize("judge_model, thinking, cap", [
    ("claude-haiku-4-5", {"type": "enabled", "budget_tokens": 1024}, 1300),
    ("claude-sonnet-5-5", {"type": "adaptive"}, 1300 + ROOM),
])
def test_the_judge_asks_each_model_for_what_it_takes(con, settings, api,
                                                     judge_model, thinking,
                                                     cap):
    s = settings.model_copy(update={"judge_pass": True,
                                    "judge_model": judge_model})
    _held(con, judge.PERSONA_REASON, "Alex is designing the context assembly.",
          "the persona block sits in the cached prefix")
    api.replies = [("TECHNICAL", "end_turn")]
    assert judge.run_pass(con, s)["relabelled"] == 1
    assert api.requests[0]["thinking"] == thinking
    assert api.requests[0]["max_tokens"] == cap


@pytest.mark.parametrize("stop", STOPS)
def test_a_cut_off_caption_is_not_stored(con, s55, api, stop):
    episodic.ingest(con, "multi-model-chat", "chat-img", [
        {"external_id": "m1", "speaker": "user",
         "content": "Here's where the canoe is at.", "created_at": TS}],
        title="Build photos")
    conv = episodic.get_conversation(con, "multi-model-chat", "chat-img")
    episodic.add_attachment(con, s55, conv["id"], "m1", "canoe.png",
                            "image/png", b"\x89PNG\r\n\x1a\n" + b"px" * 20)
    api.replies = [("A cedar canoe on two", stop)]
    assert captions.caption_pending(con, s55, conv["id"]) == {
        "captioned": 0, "skipped": 1}
    assert api.requests[0]["thinking"] == {"type": "between_tools"}
    assert api.requests[0]["max_tokens"] == 400
    assert con.execute("SELECT COUNT(*) FROM attachment_captions").fetchone()[0] == 0


def test_a_refused_profile_keeps_the_previous_one(con, settings, api):
    ledger.add_fact(con, "Alex is learning woodworking.", settings)
    api.replies = [("## Identity\n- Alex builds furniture.", "end_turn")]
    summary.regenerate(con, settings)
    before = summary.get(con)
    api.replies = [("", "refusal")]
    with pytest.raises(RuntimeError, match="stop reason refusal"):
        summary.regenerate(con, settings)
    assert summary.get(con) == before


# ------------------------------------------------------ the backfill script

@pytest.mark.parametrize("stop", STOPS)
def test_a_cut_off_backfill_batch_stays_unscored(con, settings, api,
                                                 monkeypatch, capsys, stop):
    spec = importlib.util.spec_from_file_location(
        "backfill_importance", REPO / "scripts" / "backfill_importance.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "load_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["backfill_importance.py"])
    fid = ledger.add_fact(con, "Alex keeps a workshop journal.",
                          settings)["id"]
    api.replies = [(f"{fid}=6", stop)]
    assert mod.main() == 0
    row = con.execute("SELECT importance FROM facts WHERE id=?",
                      (fid,)).fetchone()
    assert row[0] is None
    assert "unparsed-skipped 1" in capsys.readouterr().out
