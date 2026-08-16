"""#58: the judge pass — verified witnesses clear, everything else holds."""

from memory_service import episodic, judge
from memory_service.api import reason_class

SUFFIX = " — review before trusting"
GROUND = "grounding: Zephyrline not in source chat" + SUFFIX


def _conv(con, texts):
    msgs = [{"external_id": f"m{i}", "speaker": "user", "content": t,
             "created_at": 1700000000.0 + i} for i, t in enumerate(texts)]
    episodic.ingest(con, "multi-model-chat", "judge-chat", msgs, title="t")
    conv = episodic.get_conversation(con, "multi-model-chat", "judge-chat")
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM messages WHERE conversation_id=? ORDER BY id",
        (conv["id"],))]
    return conv["id"], ids


def _held(con, conv_id, msg_id, reason,
          content="Alex toured the Zephyrline factory."):
    cur = con.execute(
        "INSERT INTO facts(content, source, origin_agent, conversation_id,"
        " source_message_id, created_at, event_date, confidence,"
        " content_hash, quarantined_at, quarantine_reason)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (content, "chat", "multi-model-chat", conv_id, msg_id,
         1700000100.0, 1700000100.0, "medium",
         f"h{msg_id}-{len(reason)}", 1700000100.0, reason))
    con.commit()
    return cur.lastrowid


def _fact(con, fid):
    return con.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()


def test_verified_witness_clears(con, settings, fake_llm):
    settings.judge_pass = True
    cid, mids = _conv(con, ["I toured the zephyrline factory today", "nice"])
    fid = _held(con, cid, mids[0], GROUND)
    fake_llm["response"] = '{"Zephyrline": "toured the zephyrline factory"}'
    out = judge.run_pass(con, settings)
    assert out["cleared"] == 1
    row = _fact(con, fid)
    assert row["quarantined_at"] is None
    assert row["quarantine_reason"].startswith("cleared by judge")


def test_fabricated_span_stays_held(con, settings, fake_llm):
    settings.judge_pass = True
    cid, mids = _conv(con, ["we talked about storage boxes", "ok"])
    fid = _held(con, cid, mids[0], GROUND)
    fake_llm["response"] = '{"Zephyrline": "the zephyrline factory burned"}'
    out = judge.run_pass(con, settings)
    assert out["cleared"] == 0
    assert _fact(con, fid)["quarantined_at"] is not None


def test_unrelated_span_stays_held(con, settings, fake_llm):
    settings.judge_pass = True
    cid, mids = _conv(con, ["we had lunch at noon", "ok"])
    fid = _held(con, cid, mids[0], GROUND)
    fake_llm["response"] = '{"Zephyrline": "we had lunch at noon"}'
    out = judge.run_pass(con, settings)
    assert out["cleared"] == 0
    assert _fact(con, fid)["quarantined_at"] is not None


def test_judge_failure_stays_held(con, settings, fake_llm):
    settings.judge_pass = True
    cid, mids = _conv(con, ["I toured the zephyrline factory", "ok"])
    fid = _held(con, cid, mids[0], GROUND)
    fake_llm["fail_when_empty"] = True
    out = judge.run_pass(con, settings)
    assert out["examined"] == 1 and out["cleared"] == 0
    assert _fact(con, fid)["quarantined_at"] is not None


def test_persona_relabels_and_never_clears(con, settings, fake_llm):
    settings.judge_pass = True
    cid, mids = _conv(con, ["the persona block sits in the cached prefix"])
    fid = _held(con, cid, mids[0], judge.PERSONA_REASON,
                content="Alex is designing the context assembly.")
    fake_llm["response"] = "TECHNICAL"
    out = judge.run_pass(con, settings)
    assert out["relabelled"] == 1
    row = _fact(con, fid)
    assert row["quarantined_at"] is not None            # still held
    assert row["quarantine_reason"] == judge.JUDGED_REASON
    assert reason_class(row["quarantine_reason"]) == "source-trust-judged"
    assert fake_llm["kwargs"][-1].get("thinking_budget") == 1024


def test_persona_roleplay_verdict_keeps_label(con, settings, fake_llm):
    settings.judge_pass = True
    cid, mids = _conv(con, ["pretend you are a recruiter interviewing me"])
    fid = _held(con, cid, mids[0], judge.PERSONA_REASON)
    fake_llm["response"] = "ROLEPLAY"
    judge.run_pass(con, settings)
    row = _fact(con, fid)
    assert row["quarantined_at"] is not None
    assert row["quarantine_reason"] == judge.PERSONA_REASON


def test_disabled_by_default(con, settings, fake_llm):
    cid, mids = _conv(con, ["I toured the zephyrline factory"])
    fid = _held(con, cid, mids[0], GROUND)
    fake_llm["response"] = '{"Zephyrline": "toured the zephyrline factory"}'
    assert judge.run_pass(con, settings) == {"enabled": False}
    assert _fact(con, fid)["quarantined_at"] is not None


def test_one_attempt_per_row_per_day(con, settings, fake_llm):
    settings.judge_pass = True
    cid, mids = _conv(con, ["nothing relevant here"])
    _held(con, cid, mids[0], GROUND)
    fake_llm["response"] = "not json"
    assert judge.run_pass(con, settings)["examined"] == 1
    assert judge.run_pass(con, settings)["examined"] == 0


def test_provenance_holds_never_eligible(con, settings, fake_llm):
    settings.judge_pass = True
    cid, mids = _conv(con, ["hello there"])
    _held(con, cid, mids[0],
          "speaker-trust: no valid src= binding" + SUFFIX)
    _held(con, cid, mids[0], "external write from mcp:client" + SUFFIX)
    assert judge.run_pass(con, settings)["examined"] == 0


def test_multi_flag_rows_left_for_humans(con, settings, fake_llm):
    settings.judge_pass = True
    cid, mids = _conv(con, ["I toured the zephyrline factory"])
    fid = _held(con, cid, mids[0],
                "grounding: Zephyrline not in source chat; "
                "temporal: 2024-01-01 not stated in source chat" + SUFFIX)
    fake_llm["response"] = '{"Zephyrline": "toured the zephyrline factory"}'
    assert judge.run_pass(con, settings)["examined"] == 0
    assert _fact(con, fid)["quarantined_at"] is not None
