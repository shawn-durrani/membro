"""The miner's tags in their bracketed form (#145).

The miner's template writes every value in angle brackets (src=<N>,
importance=<1-10>), and in a 20-question benchmark run 459 of 770 fact lines
copied them. The fact lines below are real miner output from that run, over
LongMemEval's public, made-up chats, where the person is called "User". Each
test's source turn is written here to carry the words the line needs.
"""

from datetime import datetime

import pytest

from memory_service import episodic, ledger, mining
from memory_service.config import Settings

TS = 1740000000.0   # 2025-02-20, the conversation's time


@pytest.fixture
def settings(tmp_path):
    # The benchmark's cast: the person the ledger is about is "User".
    return Settings(data_dir=tmp_path / "data", user_name="User",
                    trusted_apps=["multi-model-chat"])


def _chat(con, turns, conv="chat-t"):
    """Owner and model turns alternating, one minute apart; returns the
    stored messages in order."""
    episodic.ingest(con, "multi-model-chat", conv, [
        {"external_id": f"m{i}", "speaker": "user" if i % 2 else "claude",
         "content": text, "created_at": TS + 60 * i}
        for i, text in enumerate(turns, start=1)])
    c = episodic.get_conversation(con, "multi-model-chat", conv)
    return episodic.messages_after(con, c["id"], 0)


def _only_fact(con):
    facts = (ledger.list_facts(con, status="valid")
             + ledger.list_facts(con, status="quarantined"))
    assert len(facts) == 1
    return facts[0]


def _mine_once(con, settings, fake_llm, line, conv="chat-t"):
    """One miner reply and nothing else: a second model call (the
    importance or src retry) fails the test."""
    fake_llm["queue"] = [line]
    fake_llm["fail_when_empty"] = True
    mining.distill(con, settings, "multi-model-chat", conv, regenerate=False)
    assert len(fake_llm["prompts"]) == 1


def test_bracketed_src_and_importance(con, settings, fake_llm):
    msgs = _chat(con, [
        "Hi there.", "Hello!", "Quick one about a model I'm building.",
        "Sure.", "Tell me about sentiment tools.", "Here are a few.",
        "I just finished a project analysing customer sentiment with natural "
        "language processing, and I found the work fascinating."])
    _mine_once(con, settings, fake_llm,
               "NEW src=<7> importance=<6>: User recently completed a project "
               "analyzing customer sentiment using natural language processing "
               "and found the work fascinating.")
    fact = _only_fact(con)
    assert fact["importance"] == 6
    assert fact["source_message_id"] == msgs[6]["id"]
    assert fact["quarantined_at"] is None


def test_bracketed_msg_label(con, settings, fake_llm):
    msgs = _chat(con, [
        "I'm moving to Paris for a study abroad program.", "How exciting!",
        "What should I pack?", "Layers, mostly.",
        "I'd like to volunteer while I'm in Paris, ideally with art or "
        "history organisations."])
    _mine_once(con, settings, fake_llm,
               "NEW src=<msg 5> importance=<5>: User is interested in "
               "volunteering while in Paris, with a focus on art and "
               "history-related organizations.")
    fact = _only_fact(con)
    assert fact["importance"] == 5
    assert fact["source_message_id"] == msgs[4]["id"]


def test_bracketed_src_keeps_its_event_date(con, settings, fake_llm):
    # The date is only honoured when the fact is bound to the turn that
    # states it, so an unread src= used to throw the date away as well.
    _chat(con, [
        "I sold candles at the Artisan Market at the local library on "
        "April 10th, and people loved them."])
    _mine_once(con, settings, fake_llm,
               "NEW src=<1> event=2024-04-10 importance=<6>: User attended the "
               "Artisan Market at the local library on April 10th and received "
               "positive feedback on their candles.")
    fact = _only_fact(con)
    assert fact["event_date"] == datetime(2025, 4, 10).timestamp()
    assert fact["importance"] == 6


def test_bracketed_msg_label_keeps_its_event_date(con, settings, fake_llm):
    _chat(con, [
        "I want to wear more of my vintage jewellery.", "Good plan.",
        "I tidied up my jewelry box!", "Nice.",
        "I organized my jewelry box on February 7th, finally."])
    _mine_once(con, settings, fake_llm,
               "NEW src=<msg 5> event=2025-02-07 importance=<3>: User "
               "organized their jewelry box on February 7th.")
    fact = _only_fact(con)
    assert fact["event_date"] == datetime(2025, 2, 7).timestamp()


def test_bracketed_event_date(con, settings, fake_llm):
    # No run wrapped the date itself, but the template does, so the parser
    # reads event=<YYYY-MM-DD> with a real date as well.
    _chat(con, [
        "I sold candles at the Artisan Market at the local library on "
        "April 10th, and people loved them."])
    _mine_once(con, settings, fake_llm,
               "NEW src=<1> event=<2024-04-10> importance=<6>: User attended "
               "the Artisan Market at the local library on April 10th and "
               "received positive feedback on their candles.")
    assert _only_fact(con)["event_date"] == datetime(2025, 4, 10).timestamp()


def test_copied_date_placeholder_is_no_date(con, settings, fake_llm):
    msgs = _chat(con, [
        "Hello.", "Hi!",
        "I did the Walk for Hunger charity event last Sunday and had a "
        "great time."])
    _mine_once(con, settings, fake_llm,
               "NEW src=<msg 3> event=<YYYY-MM-DD> importance=<6>: User "
               "participated in the \"Walk for Hunger\" charity event last "
               "Sunday and had a great experience.")
    fact = _only_fact(con)
    assert fact["source_message_id"] == msgs[2]["id"]
    assert fact["importance"] == 6
    # Dated to the turn's own day: the placeholder names no date.
    assert fact["event_date"] == datetime.fromtimestamp(
        msgs[2]["created_at"]).replace(hour=0, minute=0, second=0).timestamp()


def test_copied_src_placeholder_stays_unbound(con, settings, fake_llm):
    _chat(con, ["We're planning a small wedding next year."])
    _mine_once(con, settings, fake_llm,
               "NEW src=<N> importance=<7>: User is planning a small wedding "
               "next year.")
    fact = _only_fact(con)
    assert fact["source_message_id"] is None
    assert fact["importance"] == 7


def test_bracketed_supersedes(con, settings, fake_llm):
    old = ledger.add_fact(con, "User sets no budget for luxury purchases.",
                          settings, event_date=TS - 86400 * 30)
    _chat(con, [
        "I've decided to track my luxury purchases for a few months, then set "
        "a rough guideline from what I see and review it regularly."])
    _mine_once(con, settings, fake_llm,
               f"NEW src=<1> supersedes=<{old['id']}> importance=<6>: User has "
               "decided to track their luxury purchases for a few months to "
               "understand spending patterns, then set a rough guideline based "
               "on the data, and review and adjust regularly.")
    assert ledger.get_fact(con, old["id"])["invalidated_at"] is not None


def test_copied_importance_range_is_retried(con, settings, fake_llm):
    # importance=<1-10> copied whole is not a score of 1: it gets the same
    # one re-ask as a missing tag.
    _chat(con, ["I've started learning the cello."])
    fake_llm["queue"] = [
        "NEW src=<1> importance=<1-10>: User has started learning the cello.",
        "importance=<5>",
    ]
    fake_llm["fail_when_empty"] = True
    mining.distill(con, settings, "multi-model-chat", "chat-t",
                   regenerate=False)
    assert len(fake_llm["prompts"]) == 2
    assert _only_fact(con)["importance"] == 5


def test_src_retry_reads_bracketed_answer(con, settings, fake_llm):
    # A guest in the window: an unbound fact gets one batched src= retry,
    # whose template also shows src=<N or none>.
    episodic.ingest(con, "multi-model-chat", "chat-g", [
        {"external_id": "g1", "speaker": "user",
         "content": "My sister Sam is visiting next week.", "created_at": TS},
        {"external_id": "g2", "speaker": "guest:Sam",
         "content": "Hi everyone.", "created_at": TS + 60},
        {"external_id": "g3", "speaker": "user",
         "content": "I've taken up bouldering at the climbing gym.",
         "created_at": TS + 120},
    ])
    fake_llm["queue"] = [
        "NEW importance=5: User has taken up bouldering at the climbing gym.",
        "FACT 1: src=<3>",
    ]
    fake_llm["fail_when_empty"] = True
    mining.distill(con, settings, "multi-model-chat", "chat-g",
                   regenerate=False)
    fact = _only_fact(con)
    c = episodic.get_conversation(con, "multi-model-chat", "chat-g")
    assert fact["source_message_id"] == episodic.messages_after(
        con, c["id"], 0)[2]["id"]
    assert fact["quarantined_at"] is None


@pytest.mark.parametrize("attrs, src, importance, event, supersedes", [
    # Every head shape the benchmark run produced, verbatim.
    (" src=7 importance=6", "7", "6", None, None),
    (" src=<1> importance=<7>", "1", "7", None, None),
    (" src=<msg 1> importance=<7>", "1", "7", None, None),
    (" src=3 event=2023-11-01 importance=6", "3", "6", "2023-11-01", None),
    (" src=<1> event=2024-04-10 importance=<6>", "1", "6", "2024-04-10", None),
    (" src=<msg 5> event=2025-02-07 importance=<3>", "5", "3", "2025-02-07",
     None),
    (" src=<msg 3> event=<YYYY-MM-DD> importance=<6>", "3", "6", None, None),
    (" src=<1> event=<YYYY-MM-DD> importance=<7>", "1", "7", None, None),
    (" src=<5> supersedes=<12> importance=<6>", "5", "6", None, "12"),
    (" src=5 supersedes=12 importance=6", "5", "6", None, "12"),
    (" src=<N> importance=<8>", None, "8", None, None),
    (" src=8a importance=5", "8", "5", None, None),
    (" src=3 event=2005 importance=4", "3", "4", None, None),
])
def test_every_head_shape(attrs, src, importance, event, supersedes):
    def val(rx):
        m = rx.search(attrs)
        return m.group(1) if m else None
    assert val(mining._SRC_RE) == src
    assert val(mining._IMPORTANCE_RE) == importance
    assert val(mining._EVENT_RE) == event
    assert val(mining._SUPERSEDES_RE) == supersedes
