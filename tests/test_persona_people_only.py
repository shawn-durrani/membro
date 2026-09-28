"""The roleplay check reads what people said, never a model's advice (#148).

The case from the 29 September benchmark run: the person said they were an
aspiring stand-up comedian, the AI's tips said "Rehearse your set", and every
fact from the chat was held as roleplay."""

from memory_service import episodic, ledger, mining

FACT = ("NEW src=1 importance=7: Alex is an aspiring stand-up comedian and is "
        "working on improving their comedy craft.")


def _mine(con, settings, fake_llm, turns, line=FACT):
    episodic.ingest(con, "multi-model-chat", "chat-p", [
        {"external_id": f"p{i}", "speaker": speaker, "content": text,
         "created_at": 1700000000.0 + 60 * i}
        for i, (speaker, text) in enumerate(turns, start=1)])
    fake_llm["response"] = line
    return mining.distill(con, settings, "multi-model-chat", "chat-p",
                          regenerate=False)


def test_models_advice_does_not_hold_the_persons_facts(con, settings, fake_llm):
    res = _mine(con, settings, fake_llm, [
        ("user", "As an aspiring stand-up comedian, I'm looking for advice on "
                 "how to improve my craft."),
        ("claude", "Tips for recording: rehearse your set so you're "
                   "comfortable with the material before you film it."),
    ])
    assert res == {"added": 1, "quarantined": 0}
    assert len(ledger.list_facts(con, status="valid")) == 1


def test_the_persons_own_framing_still_holds(con, settings, fake_llm):
    res = _mine(con, settings, fake_llm, [
        ("user", "Let's rehearse my set. Pretend you're the audience at an "
                 "open mic: I'm an aspiring stand-up comedian."),
        ("claude", "Ready when you are."),
    ])
    assert res == {"added": 1, "quarantined": 1}
    held = ledger.review_queue(con)[0]
    assert "source-trust" in held["quarantine_reason"]


def test_a_guests_framing_still_holds(con, settings, fake_llm):
    res = _mine(con, settings, fake_llm, [
        ("user", "I'm an aspiring stand-up comedian, working on my craft."),
        ("guest:Sam", "Pretend you're a talent scout watching us."),
    ])
    assert res["quarantined"] == 1
    assert "source-trust" in ledger.review_queue(con)[0]["quarantine_reason"]


def test_an_unrecognised_speakers_framing_still_holds(con, settings, fake_llm):
    # Not known to be a model seat, so its words still count (fail safe).
    res = _mine(con, settings, fake_llm, [
        ("user", "I'm an aspiring stand-up comedian, working on my craft."),
        ("agent:scribe", "Mock interview transcript follows."),
    ])
    assert res["quarantined"] == 1
    assert "source-trust" in ledger.review_queue(con)[0]["quarantine_reason"]
