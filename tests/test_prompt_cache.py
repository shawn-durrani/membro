"""Prompt caching: where the breakpoints go, and that the prefix they close
is the same bytes from one call to the next. The miner's list of existing
facts also keeps to the chat's scope, checked here in the same requests.

Every request here goes through the real utility_complete into a stand-in
Anthropic client, so what is checked is the request body the API would get.
"""

import logging
import re
import time

import pytest

from memory_service import episodic, ledger, llm, mining, recall, summary

_real_recall = recall.recall   # the api fixture stubs it; the scope tests put it back


class _Usage:
    def __init__(self, **kw):
        self.input_tokens = kw.get("input", 0)
        self.cache_creation_input_tokens = kw.get("write", 0)
        self.cache_read_input_tokens = kw.get("read", 0)
        self.output_tokens = kw.get("output", 0)


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Anthropic:
    """Records every messages.create call and answers from a queue."""

    def __init__(self):
        self.requests = []
        self.replies = []
        self.usage = _Usage(input=10, write=20, read=30, output=5)
        self.messages = self

    def create(self, **req):
        self.requests.append(req)

        class _Resp:
            pass
        resp = _Resp()
        resp.content = [_Text(self.replies.pop(0) if self.replies else "NONE")]
        resp.usage = self.usage
        return resp


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    fake = _Anthropic()
    monkeypatch.setattr(llm, "_clients", {"anthropic": fake})
    monkeypatch.setattr(llm, "_tally", {})
    monkeypatch.setattr(mining.recall, "recall", lambda *a, **k: [])
    return fake


def _in_burst(monkeypatch):
    monkeypatch.setattr(mining, "_last_mining_call", time.monotonic())


def _alone(monkeypatch):
    monkeypatch.setattr(mining, "_last_mining_call", float("-inf"))


def _blocks(req):
    """(system blocks, user content blocks) of one request."""
    return req.get("system", []), req["messages"][0]["content"]


def _marked(blocks):
    return [i for i, b in enumerate(blocks) if "cache_control" in b]


def _cached_prefix(req) -> str:
    """The text up to the last breakpoint, system first."""
    system, content = _blocks(req)
    blocks = list(system) + list(content)
    last = max(_marked(blocks))
    return "".join(b["text"] for b in blocks[:last + 1])


def _conversation(con, name, text="I booked the kitchen renovation for spring."):
    episodic.ingest(con, "multi-model-chat", name, [
        {"external_id": "m1", "speaker": "user", "content": text,
         "created_at": 1700000000.0}], title=name)


def _ledger(con, settings, n):
    for i in range(n):
        ledger.add_fact(con, f"Alex keeps note number {i} about the garden.",
                        settings)


# ---------------------------------------------------------------- llm layer

def test_a_plain_prompt_is_sent_as_before(settings, api):
    llm.utility_complete("hello", settings)
    req = api.requests[0]
    assert req["messages"] == [{"role": "user", "content": "hello"}]
    assert "system" not in req


def test_parts_put_breakpoints_only_where_marked(settings, api):
    llm.utility_complete(
        [{"text": "shared ", "cache": "5m"}, {"text": "", "cache": "5m"},
         {"text": "long ", "cache": "1h"}, {"text": "varying"}],
        settings, system=[{"text": "rules", "cache": "1h"}])
    system, content = _blocks(api.requests[0])
    assert system == [{"type": "text", "text": "rules",
                       "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    assert content == [
        {"type": "text", "text": "shared ", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "long ",
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": "varying"},
    ]   # the empty part is dropped: the API refuses an empty block


def test_an_unknown_ttl_is_refused(settings, api):
    with pytest.raises(ValueError):
        llm.utility_complete([{"text": "x", "cache": "10m"}], settings)


def test_usage_is_logged_per_site_without_content(settings, api, caplog):
    caplog.set_level(logging.INFO, logger="memory_service.llm")
    llm.utility_complete("a secret sentence about the garden", settings,
                         site="miner")
    llm.utility_complete("another one", settings, site="miner")
    lines = [r.getMessage() for r in caplog.records
             if r.name == "memory_service.llm"]
    assert lines[0].startswith(
        "model call miner on claude-haiku-4-5: input 10, cache write 20, "
        "cache read 30, output 5.")
    assert "2 calls, 50% of input read from cache" in lines[1]
    assert not any("garden" in line or "another" in line for line in lines)
    assert llm.usage_totals()["miner"] == {
        "calls": 2, "input": 20, "cache_write": 40, "cache_read": 60,
        "output": 10}


def test_the_usage_log_gets_one_handler(monkeypatch):
    monkeypatch.setattr(llm.log, "handlers", [])
    llm.enable_usage_log()
    llm.enable_usage_log()
    assert len(llm.log.handlers) == 1
    assert llm.log.level == logging.INFO


def test_the_compatible_branch_joins_parts_and_logs_cached_tokens(
        tmp_path, monkeypatch, caplog):
    from memory_service.config import Settings

    class _OpenAI:
        last = {}

        def __init__(self, **kw):
            self.chat = self
            self.completions = self

        def create(self, **req):
            _OpenAI.last = req

            class _Details:
                cached_tokens = 70

            class _Usage:
                prompt_tokens = 100
                completion_tokens = 4
                prompt_tokens_details = _Details()

            class _Msg:
                content = "ok"

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]
                usage = _Usage()
            return _Resp()

    monkeypatch.setattr(llm, "_clients", {})
    monkeypatch.setattr(llm, "_tally", {})
    monkeypatch.setattr(llm, "OpenAI", _OpenAI)
    caplog.set_level(logging.INFO, logger="memory_service.llm")
    s = Settings(data_dir=tmp_path / "d", llm_base_url="http://127.0.0.1:1/v1")
    llm.utility_complete([{"text": "a ", "cache": "5m"}, {"text": "b"}], s,
                         model="qwen3", system="rules", site="miner")
    assert _OpenAI.last["messages"] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "a b"}]
    assert "input 30, cache write 0, cache read 70, output 4" in caplog.text


# ---------------------------------------------------------------- the miner

def test_miner_marks_the_instructions_and_the_window(con, settings, api,
                                                     monkeypatch):
    _ledger(con, settings, 100)          # ids 1-100: 1-95 sealed, 96-100 open
    _conversation(con, "c1")
    _in_burst(monkeypatch)
    mining.distill(con, settings, "multi-model-chat", "c1", regenerate=False)
    system, content = _blocks(api.requests[0])
    assert len(system) == 1 and system[0]["cache_control"] == {"type": "ephemeral"}
    assert "permanent memory ledger about Alex" in system[0]["text"]
    assert "Existing valid entries" not in system[0]["text"]
    # Buckets of 32 ids: 1-31, 32-63, 64-95 sealed, then 96-100 open, then
    # the tail. Breakpoints on the last sealed bucket and on the open facts.
    assert len(content) == 5
    assert _marked(content) == [2, 3]
    assert content[0]["text"].startswith("## Existing valid entries")
    assert content[2]["text"].endswith("note number 94 about the garden.\n")
    assert content[3]["text"].startswith("- [96] ")
    assert "## Conversation excerpt\n[msg 1] Alex: I booked" in content[4]["text"]
    assert len(_marked(system) + _marked(content)) <= 4


def test_a_lone_mining_call_marks_nothing(con, settings, api, monkeypatch):
    """Crossband hands chats over one at a time, minutes apart. A write
    nothing reads within five minutes only costs a quarter more."""
    _ledger(con, settings, 100)
    _conversation(con, "c1")
    _alone(monkeypatch)
    mining.distill(con, settings, "multi-model-chat", "c1", regenerate=False)
    system, content = _blocks(api.requests[0])
    assert _marked(system) == [] and _marked(content) == []


def test_a_backlog_marks_its_first_chunk(con, settings, api, monkeypatch):
    monkeypatch.setattr(mining, "CHUNK_MSGS", 1)
    _ledger(con, settings, 100)
    episodic.ingest(con, "multi-model-chat", "long", [
        {"external_id": f"m{i}", "speaker": "user",
         "content": f"Turn {i} about the kitchen renovation plans.",
         "created_at": 1700000000.0 + i} for i in range(2)], title="long")
    _alone(monkeypatch)
    mining.distill(con, settings, "multi-model-chat", "long", regenerate=False)
    assert len(api.requests) == 2
    for req in api.requests:   # another chunk follows, then a burst is on
        assert _marked(_blocks(req)[1])


def test_miner_prefix_is_byte_identical_across_calls(con, settings, api,
                                                     monkeypatch):
    """A fact mined between two calls lands in the open part, after the
    breakpoint, so the second call reads the first call's prefix whole."""
    _ledger(con, settings, 100)
    _conversation(con, "c1")
    _conversation(con, "c2", "We picked the tiles for the kitchen at last.")
    _in_burst(monkeypatch)
    api.replies = ["NEW src=1 importance=5: Alex booked the kitchen renovation "
                   "for spring."]
    mining.distill(con, settings, "multi-model-chat", "c1", regenerate=False)
    mining.distill(con, settings, "multi-model-chat", "c2", regenerate=False)
    first, second = api.requests
    s1, c1 = _blocks(first)
    s2, c2 = _blocks(second)
    assert s1 == s2
    assert c1[:3] == c2[:3]                       # the sealed buckets
    assert c2[3]["text"].startswith(c1[3]["text"])   # the new fact appends
    assert "Alex booked the kitchen renovation" in c2[3]["text"]


def test_a_sealed_bucket_is_appended_as_its_own_part(con, settings, api,
                                                     monkeypatch):
    """When ids fill a bucket it seals as a new part after the old ones, so
    the next call finds the last call's breakpoint one part back."""
    _ledger(con, settings, 94)            # ids 1-94: two sealed buckets
    _conversation(con, "c1")
    _conversation(con, "c2", "We picked the tiles for the kitchen at last.")
    _in_burst(monkeypatch)
    mining.distill(con, settings, "multi-model-chat", "c1", regenerate=False)
    _ledger(con, settings, 0)
    for i in range(3):                    # ids 95-97 seal the 64-95 bucket
        ledger.add_fact(con, f"Alex keeps extra note {i} about the shed.",
                        settings)
    mining.distill(con, settings, "multi-model-chat", "c2", regenerate=False)
    c1 = _blocks(api.requests[0])[1]
    c2 = _blocks(api.requests[1])[1]
    assert _marked(c1) == [1, 2]
    assert _marked(c2) == [2, 3]
    assert [b["text"] for b in c1[:2]] == [b["text"] for b in c2[:2]]
    assert "cache_control" not in c2[1]   # the old breakpoint's text, unmarked


def test_the_window_holds_the_newest_facts_oldest_first(con, settings, api,
                                                        monkeypatch):
    monkeypatch.setattr(mining, "WINDOW_FACTS", 20)
    _ledger(con, settings, 70)
    sealed, open_facts = mining._fact_window(con)
    ids = [f["id"] for b in sealed for f in b] + [f["id"] for f in open_facts]
    assert ids == sorted(ids)
    newest = {f["id"] for f in ledger.list_facts(con, status="valid", limit=20)}
    assert newest <= set(ids)             # every one of the newest 20
    assert ids[0] % mining.WINDOW_STEP == 0   # from a stepped floor
    assert len(ids) < 20 + mining.WINDOW_STEP


def test_older_on_topic_facts_ride_after_the_breakpoint(con, settings, api,
                                                        monkeypatch):
    monkeypatch.setattr(mining, "WINDOW_FACTS", 20)
    old = ledger.add_fact(con, "Alex works at Globex as an architect.", settings,
                          event_date=1690000000.0)   # before the chat
    _ledger(con, settings, 100)
    monkeypatch.setattr(mining.recall, "recall",
                        lambda *a, **k: [ledger.get_fact(con, old["id"])])
    _conversation(con, "c1", "I left Globex for Initech.")
    _in_burst(monkeypatch)
    api.replies = [f"NEW src=1 supersedes={old['id']} importance=6: Alex left "
                   "Globex for Initech."]
    mining.distill(con, settings, "multi-model-chat", "c1", regenerate=False)
    content = _blocks(api.requests[0])[1]
    tail = content[-1]["text"]
    assert tail.startswith(f"- [{old['id']}] Alex works at Globex")
    assert "cache_control" not in content[-1]
    assert ledger.get_fact(con, old["id"])["invalidated_at"] is not None


_VOLATILE = [
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    re.compile(r"\b\d{10}(\.\d+)?\b"),                 # an epoch timestamp
    re.compile(r"\b\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2})?"),  # a date or time
    re.compile(r"\{\s*\""),                            # serialised JSON
]


def test_the_cached_prefix_carries_nothing_volatile(con, settings, api,
                                                    monkeypatch):
    """No clock, uuid or serialised object sits before the last breakpoint,
    and a later build of the same ledger is the same bytes."""
    _ledger(con, settings, 100)
    _conversation(con, "c1")
    _conversation(con, "c2")
    _in_burst(monkeypatch)
    mining.distill(con, settings, "multi-model-chat", "c1", regenerate=False)
    monkeypatch.setattr(time, "time", lambda: 2_000_000_000.0)
    _in_burst(monkeypatch)
    mining.distill(con, settings, "multi-model-chat", "c2", regenerate=False)
    first, second = (_cached_prefix(r) for r in api.requests)
    assert first == second
    # The system text is a pure function of the owner's name: its example
    # dates are fixed wording, and nothing else is interpolated.
    instructions = mining._miner_instructions(settings.user_name)
    assert _blocks(api.requests[0])[0][0]["text"] == instructions
    entries = first[len(instructions):]
    for pattern in _VOLATILE:
        assert not pattern.search(entries), pattern.pattern
    listed = [int(i) for i in re.findall(r"^- \[(\d+)\]", entries, re.M)]
    assert listed == sorted(listed) and len(listed) == 100


# ---------------------------------------------------------------- the chat's scope

APP = "multi-model-chat"


def _say(con, chat, speaker, text):
    """One more turn in `chat`. Returns (the chat's id, the turn's id)."""
    n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    episodic.ingest(con, APP, chat, [
        {"external_id": f"t{n}", "speaker": speaker, "content": text,
         "created_at": 1700000000.0 + n}], title=chat)
    row = con.execute("SELECT id, conversation_id FROM messages "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    return row["conversation_id"], row["id"]


def _guest_fact(con, settings, chat, text, **kw):
    """A guest's fact drawn from their own turn in `chat`, as it stands once
    approved: valid, and bound to that chat."""
    cid, mid = _say(con, chat, "guest:Sam", "Sam here, just dropping in.")
    f = ledger.add_fact(con, text, settings, source="chat", origin_agent=APP,
                        source_app=APP, conversation_id=cid,
                        source_message_id=mid, **kw)
    assert f["scope"] == "conversation" and not f["quarantined"]
    return f["id"]


def _text(req) -> str:
    system, content = _blocks(req)
    return "".join(b["text"] for b in list(system) + list(content))


def _listed(req) -> list[int]:
    return [int(i) for i in re.findall(r"^- \[(\d+)\]", _text(req), re.M)]


def test_another_chats_guest_fact_never_reaches_the_miner(con, settings, api,
                                                          monkeypatch):
    """Not in the recency window, not among the on-topic facts, and a fact
    said here can't mark it replaced."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(mining.recall, "recall", _real_recall)
    _ledger(con, settings, 40)                           # ids 1-40
    theirs = _guest_fact(con, settings, "room-b",
                         "Sam (a guest) is planning a kitchen renovation in "
                         "Fairhaven.", event_date=1690000000.0)   # the newest
    _say(con, "c1", "user", "I booked the kitchen renovation for spring.")
    api.replies = [f"NEW src=1 supersedes={theirs} importance=5: Alex booked "
                   "the kitchen renovation for spring."]
    mining.distill(con, settings, APP, "c1", regenerate=False)
    req = api.requests[0]
    assert "Fairhaven" not in _text(req)
    assert _listed(req) == list(range(1, 41))            # the owner's facts, all
    assert ledger.get_fact(con, theirs)["invalidated_at"] is None


def test_this_chats_guest_facts_reach_its_own_miner(con, settings, api,
                                                    monkeypatch):
    """A recent one rides the recency window. One older than the window's
    floor is still reached by the topic search, which names this chat."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(mining.recall, "recall", _real_recall)
    monkeypatch.setattr(mining, "WINDOW_FACTS", 20)
    older = _guest_fact(con, settings, "c1",
                        "Sam (a guest) is renovating a kitchen too.")    # id 1
    _ledger(con, settings, 100)                          # ids 2-101, floor 64
    recent = _guest_fact(con, settings, "c1",
                         "Sam (a guest) is a carpenter by trade.")       # id 102
    _say(con, "c1", "user", "I booked the kitchen renovation for spring.")
    _in_burst(monkeypatch)
    mining.distill(con, settings, APP, "c1", regenerate=False)
    content = _blocks(api.requests[0])[1]
    assert content[-2]["text"].endswith(
        f"- [{recent}] Sam (a guest) is a carpenter by trade.\n")
    assert content[-1]["text"].startswith(
        f"- [{older}] Sam (a guest) is renovating a kitchen too.\n")


def test_every_chat_counts_its_window_from_the_same_floor(con, settings,
                                                          monkeypatch):
    """The floor counts global facts only. A chat with many facts of its own
    starts where every other chat does, so they share the first buckets."""
    monkeypatch.setattr(mining, "WINDOW_FACTS", 20)
    _ledger(con, settings, 64)                           # ids 1-64
    room, _ = _say(con, "room", "user", "Sam is staying over this week.")
    other, _ = _say(con, "other", "user", "Nothing much to report today.")
    for i in range(36):                                  # ids 65-100
        ledger.add_fact(con, f"Sam (a guest) mentioned plant pot number {i}.",
                        settings, conversation_id=room, scope="conversation")
    ours, theirs = mining._fact_window(con, room), mining._fact_window(con, other)
    assert ours[0][0] == theirs[0][0]                    # ids 32-63
    assert ours[0][0][0]["id"] == 32
    bound = {f["id"] for b in ours[0] for f in b} | {f["id"] for f in ours[1]}
    assert set(range(65, 101)) <= bound
    seen = {f["id"] for b in theirs[0] for f in b} | {f["id"] for f in theirs[1]}
    assert seen == set(range(32, 65))


def test_the_prefix_holds_across_calls_in_a_chat_with_guest_facts(
        con, settings, api, monkeypatch):
    """Between two calls in one chat, another chat gains a guest fact, the
    owner gains a fact and this chat gains one. The sealed buckets, this
    chat's own fact among them, are the same bytes, and the open part only
    grows at its end."""
    _ledger(con, settings, 40)                           # ids 1-40
    ours = _guest_fact(con, settings, "c1",
                       "Sam (a guest) is a carpenter by trade.")         # id 41
    for i in range(30):                                  # ids 42-71
        ledger.add_fact(con, f"Alex keeps later note {i} about the shed.",
                        settings)
    _say(con, "c1", "user", "I booked the kitchen renovation for spring.")
    _in_burst(monkeypatch)
    mining.distill(con, settings, APP, "c1", regenerate=False)
    _guest_fact(con, settings, "room-b",
                "Sam (a guest) grows tomatoes in Fairhaven.")            # id 72
    ledger.add_fact(con, "Alex keeps a spare key under the mat.", settings)
    also_ours = _guest_fact(con, settings, "c1",
                            "Sam (a guest) is building a bookshelf.")    # id 74
    _say(con, "c1", "user", "We picked the tiles for the kitchen at last.")
    mining.distill(con, settings, APP, "c1", regenerate=False)
    first, second = api.requests
    s1, c1 = _blocks(first)
    s2, c2 = _blocks(second)
    assert s1 == s2
    assert _marked(c1) == _marked(c2) == [1, 2]
    assert c1[:2] == c2[:2]                              # ids 1-31, 32-63
    assert f"- [{ours}] Sam (a guest) is a carpenter" in c1[1]["text"]
    assert c2[2]["text"] == c1[2]["text"] + (
        "- [73] Alex keeps a spare key under the mat.\n"
        f"- [{also_ours}] Sam (a guest) is building a bookshelf.\n")
    assert "Fairhaven" not in _text(second)


# ---------------------------------------------------------------- the summary

def _summary_ledger(con, settings, n=12, words=12):
    for i in range(n):
        ledger.add_fact(con, f"Alex worked on garden bed number {i} " +
                        "with care " * (words // 2), settings, importance=6)


def test_summary_draft_and_expansion_share_a_cached_prefix(con, settings, api):
    s = settings.model_copy(update={"memory_summary_words": 100,
                                    "memory_summary_fill": 0.8})
    _summary_ledger(con, s)
    api.replies = ["## Identity\n- short", "## Identity\n" + "word " * 90]
    summary.regenerate(con, s)
    draft, expand = api.requests
    assert draft["model"] == expand["model"] == s.summary_model
    d, e = _blocks(draft)[1], _blocks(expand)[1]
    assert d[0] == e[0]
    assert d[0]["cache_control"] == {"type": "ephemeral"}
    assert "## Durable entries\n" in d[0]["text"]
    assert "## Active entries\n" in d[0]["text"]
    assert "Write a structured profile" in d[1]["text"]
    assert "The profile below is 4 words" in e[1]["text"]
    assert _marked(d) == [0] and _marked(e) == [0]


def test_a_summary_that_cannot_expand_marks_nothing(con, settings, api):
    """Too few entries to fill the floor: no expansion will read the cache,
    so the draft doesn't pay to write it."""
    s = settings.model_copy(update={"memory_summary_words": 2000})
    _summary_ledger(con, s, n=3)
    api.replies = ["## Identity\n- short"]
    summary.regenerate(con, s)
    assert len(api.requests) == 1
    assert _marked(_blocks(api.requests[0])[1]) == []
