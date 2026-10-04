"""Claude Code sessions, summarised into memory (#206).

A session file is read once it has been quiet for half an hour. The model
sees only the prompts typed and the replies written, masked, and its
summary is kept as a `claude-code` conversation and mined. How far each
session was read lives in its own table, so an erased summary stays
erased.
"""

import json
import os
import threading
import time

import pytest

from memory_service import (busy, claude_code_feed as feed, db as db_mod,
                            episodic, erasers, llm, mining, walls)
from memory_service.config import Settings

T0 = 1_790_000_000.0                       # a fixed point in October 2026
NOW = T0 + 3 * 3600


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def prompt(text, ts, **extra):
    return {"type": "user", "timestamp": _iso(ts), "origin": {"kind": "human"},
            "message": {"role": "user", "content": text}, **extra}


def reply(text, ts, **extra):
    return {"type": "assistant", "timestamp": _iso(ts),
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}, **extra}


def tool_call(ts):
    return {"type": "assistant", "timestamp": _iso(ts), "message": {
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}


def tool_output(text, ts):
    return {"type": "user", "timestamp": _iso(ts), "message": {
        "role": "user", "content": [{"type": "tool_result", "content": text}]}}


@pytest.fixture
def feed_settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", user_name="Alex",
                    claude_code_feed=True,
                    claude_code_dir=str(tmp_path / "projects"),
                    trusted_apps=["claude-code"])


@pytest.fixture
def fcon(feed_settings):
    db_mod.init(feed_settings)
    c = db_mod.connect(feed_settings.db_path)
    yield c
    c.close()


def write_session(settings, entries, session_id="s-1", folder="-garden",
                  mtime=T0 + 3600, entrypoint="claude-desktop",
                  cwd="/Users/you/dev/garden-planner", append=False):
    path = feed.sessions_dir(settings) / folder / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if append else "w") as fh:
        for e in entries:
            e = {"sessionId": session_id, "entrypoint": entrypoint, "cwd": cwd, **e}
            fh.write(json.dumps(e) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def a_session(settings, **kw):
    return write_session(settings, [
        prompt("Add a watering schedule to the garden planner", T0),
        tool_call(T0 + 10),
        tool_output("total 8 drwxr-xr-x schedule.py", T0 + 11),
        reply("Added a weekly watering schedule with reminders.", T0 + 60),
        {"type": "custom-title", "customTitle": "Watering schedule"},
        {"type": "pr-link", "timestamp": _iso(T0 + 50),
         "prUrl": "https://github.com/example/garden-planner/pull/7"},
    ], **kw)


def summaries(con, session_id="s-1"):
    return [dict(r) for r in con.execute(
        "SELECT m.external_id, m.speaker, m.content, m.created_at, c.title "
        "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.source_app='claude-code' AND c.external_id=? ORDER BY m.id",
        (session_id,))]


# ---- what the model sees ----

def test_only_typed_prompts_and_written_replies_are_read(feed_settings):
    path = write_session(feed_settings, [
        prompt("Plan the herb bed", T0),
        prompt("<task-notification>agent finished</task-notification>", T0 + 1,
               origin={"kind": "task-notification"}),
        prompt("Caveat: the messages below were generated", T0 + 2, isMeta=True),
        prompt("This session is being continued from a previous one", T0 + 3,
               isCompactSummary=True),
        prompt("<command-name>/model</command-name>", T0 + 4),
        prompt("<system-reminder>be brief</system-reminder>\nAnd the rosemary", T0 + 5),
        tool_output("rosemary.txt", T0 + 6),
        tool_call(T0 + 7),
        reply("Mapped the herb bed.", T0 + 8),
        reply("A side thread", T0 + 9, isSidechain=True),
    ])
    s = feed.read_session(path)
    assert [(t.role, t.text) for t in s.turns] == [
        ("you", "Plan the herb bed"),
        ("you", "And the rosemary"),
        ("assistant", "Mapped the herb bed."),
    ]


def test_the_title_and_project_come_from_the_session(feed_settings):
    s = feed.read_session(a_session(
        feed_settings, cwd="/Users/you/dev/garden-planner/.claude/worktrees/brave-fox"))
    assert s.title == "Watering schedule"
    assert s.project == "garden-planner"
    assert s.pull_requests == [(T0 + 50, "https://github.com/example/garden-planner/pull/7")]


def test_a_session_with_no_title_is_named_from_its_first_prompt(feed_settings):
    first = "Sort out the compost rota for the community garden this spring"
    s = feed.read_session(write_session(feed_settings, [prompt(first, T0)]))
    assert s.title == first[:60] + "..."


def test_secrets_are_masked_before_the_model_sees_them(feed_settings):
    key = "sk-" + "ant-" + "a1B2" * 8
    token = "Zx9" + "kQ2m" * 10
    s = feed.read_session(write_session(feed_settings, [
        prompt(f"Use {key} for the API", T0),
        prompt(f"export MEMORY_AUTH_TOKEN={token}", T0 + 1),
        prompt(f"curl -H 'Authorization: Bearer {token}'", T0 + 2),
        reply("Commit 3f2a9c1e5b7d4f6a8c0e2b4d6f8a0c2e4b6d8f0a is in.", T0 + 3),
    ]))
    text, _ = feed.digest(s, 0.0, "Alex")
    assert key not in text and token not in text
    assert text.count(feed.MASK) == 3
    assert "MEMORY_AUTH_TOKEN=" in text
    assert "3f2a9c1e5b7d4f6a8c0e2b4d6f8a0c2e4b6d8f0a" in text   # a commit, not a key


def test_a_long_session_keeps_its_opening_and_its_latest_turns(feed_settings):
    entries = [prompt(f"step {i} " + "x" * 2900, T0 + i) for i in range(80)]
    s = feed.read_session(write_session(feed_settings, entries))
    text, turns = feed.digest(s, 0.0, "Alex")
    assert len(text) <= feed.DIGEST_CHARS + 100
    assert "step 0 " in text and "step 79 " in text
    assert "turns left out here]" in text
    assert len(turns) == 80


# ---- the sweep ----

def test_a_quiet_session_is_summarised_kept_and_mined(fcon, feed_settings, fake_llm):
    a_session(feed_settings)
    fake_llm["queue"] = [
        "Alex added a weekly watering schedule to the garden planner.",
        "NEW src=1 importance=5: Alex is building a garden planner app.",
    ]
    out = feed.sweep(fcon, feed_settings, now=NOW)
    assert out["summarised"] == 1 and out["facts_added"] == 1

    seen = fake_llm["prompts"][0]
    assert "Add a watering schedule" in seen
    assert "Added a weekly watering schedule" in seen
    assert "drwxr-xr-x" not in seen                     # tool output stays out
    assert fake_llm["requests"][0]["site"] == "claude-code"

    (row,) = summaries(fcon)
    assert row["speaker"] == "record:claude-code"
    assert row["title"] == "Watering schedule"
    assert row["created_at"] == T0 + 60                 # dated to the work
    assert row["content"].startswith("A coding session in garden-planner, on ")
    assert "Alex added a weekly watering schedule" in row["content"]
    assert "Pull requests: https://github.com/example/garden-planner/pull/7" in row["content"]
    miner = fake_llm["prompts"][1]
    assert "[msg 1] Record of Alex's activity: A coding session" in miner
    assert "RECORDS:" in miner
    fact = fcon.execute("SELECT content, quarantined_at FROM facts").fetchone()
    assert fact["content"] == "Alex is building a garden planner app."
    assert fact["quarantined_at"] is None               # trusted app, walls passed


def test_an_untrusted_app_s_record_is_held(fcon, feed_settings, fake_llm):
    settings = feed_settings.model_copy(update={"trusted_apps": []})
    a_session(settings)
    fake_llm["queue"] = [
        "Alex added a weekly watering schedule to the garden planner.",
        "NEW src=1 importance=5: Alex is building a garden planner app.",
    ]
    feed.sweep(fcon, settings, now=NOW)
    assert fcon.execute("SELECT quarantined_at FROM facts").fetchone()[0] is not None


# ---- the record speaker class ----

def test_a_record_is_its_own_speaker_class():
    assert walls.speaker_class("record:claude-code") == "record"
    assert walls.speaker_class("Record:claude-code") == "record"
    assert walls.speaker_class("record:") == "unrecognised"     # whose record?
    assert walls.speaker_trust_flag("record:claude-code") is None


def test_a_chat_with_no_record_gets_no_record_rules(con, settings, fake_llm,
                                                    sample_conversation):
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    assert "RECORDS:" not in fake_llm["prompts"][0]


def test_a_session_still_in_use_waits(fcon, feed_settings, fake_llm):
    a_session(feed_settings, mtime=NOW - 5 * 60)
    assert feed.sweep(fcon, feed_settings, now=NOW)["summarised"] == 0
    assert fake_llm["prompts"] == []
    fake_llm["queue"] = ["Alex added a watering schedule.", "NONE"]
    assert feed.sweep(fcon, feed_settings, now=NOW + 26 * 60)["summarised"] == 1


def test_an_sdk_session_is_skipped(fcon, feed_settings, fake_llm):
    a_session(feed_settings, entrypoint="sdk-py")
    out = feed.sweep(fcon, feed_settings, now=NOW)
    assert out == {"summarised": 0, "nothing_new": 0, "skipped": 1,
                   "failed": 0, "facts_added": 0}
    assert fake_llm["prompts"] == [] and summaries(fcon) == []


def test_a_file_read_before_and_untouched_is_not_read_again(fcon, feed_settings,
                                                            fake_llm, monkeypatch):
    a_session(feed_settings)
    fake_llm["queue"] = ["Alex added a watering schedule.", "NONE"]
    feed.sweep(fcon, feed_settings, now=NOW)
    monkeypatch.setattr(feed, "read_session",
                        lambda p: pytest.fail("an unchanged file was opened"))
    assert feed.sweep(fcon, feed_settings, now=NOW + 600)["summarised"] == 0


def test_a_resumed_session_gets_a_summary_of_the_new_part_only(fcon, feed_settings,
                                                               fake_llm):
    a_session(feed_settings)
    fake_llm["queue"] = ["Alex added a weekly watering schedule.", "NONE"]
    feed.sweep(fcon, feed_settings, now=NOW)
    write_session(feed_settings, [
        prompt("Now add frost warnings", T0 + 7200),
        reply("Frost warnings now show on cold nights.", T0 + 7260),
    ], append=True, mtime=T0 + 7300)
    fake_llm["queue"] = ["Alex added frost warnings to the garden planner.", "NONE"]
    assert feed.sweep(fcon, feed_settings, now=T0 + 7300 + 3600)["summarised"] == 1

    second = fake_llm["prompts"][2]
    assert "Now add frost warnings" in second
    assert "Add a watering schedule" not in second            # old turns aren't resent
    assert "Alex added a weekly watering schedule." in second  # the last summary, as context
    rows = summaries(fcon)
    assert len(rows) == 2
    assert "frost warnings" in rows[1]["content"]
    assert "Pull requests" not in rows[1]["content"]          # that link was the first part's


def test_an_erased_summary_stays_erased(fcon, feed_settings, fake_llm):
    a_session(feed_settings)
    fake_llm["queue"] = ["Alex added a watering schedule.", "NONE"]
    feed.sweep(fcon, feed_settings, now=NOW)
    (mid,) = [r["id"] for r in fcon.execute("SELECT id FROM messages")]
    erasers.erase_message(fcon, mid)
    fcon.commit()
    a_session(feed_settings, mtime=T0 + 3700)                # touched, nothing new
    fake_llm["queue"] = ["Alex added a watering schedule.", "NONE"]
    out = feed.sweep(fcon, feed_settings, now=NOW + 600)
    assert out["summarised"] == 0 and len(fake_llm["prompts"]) == 2
    assert summaries(fcon) == []


def test_nothing_worth_keeping_stores_nothing_and_isnt_asked_again(fcon, feed_settings,
                                                                   fake_llm):
    write_session(feed_settings, [prompt("hi", T0), reply("Hello!", T0 + 5)])
    fake_llm["queue"] = ["NONE"]
    out = feed.sweep(fcon, feed_settings, now=NOW)
    assert out["nothing_new"] == 1 and summaries(fcon) == []
    write_session(feed_settings, [prompt("hi", T0), reply("Hello!", T0 + 5)],
                  mtime=T0 + 3700)
    feed.sweep(fcon, feed_settings, now=NOW + 600)
    assert len(fake_llm["prompts"]) == 1


def test_a_failed_summary_is_tried_again_next_sweep(fcon, feed_settings, fake_llm):
    a_session(feed_settings)
    fake_llm["fail_when_empty"] = True
    assert feed.sweep(fcon, feed_settings, now=NOW)["failed"] == 1
    fake_llm["queue"] = ["Alex added a watering schedule.", "NONE"]
    assert feed.sweep(fcon, feed_settings, now=NOW + 300)["summarised"] == 1


def test_a_missing_key_stops_the_sweep_and_marks_nothing_read(fcon, feed_settings,
                                                              monkeypatch):
    a_session(feed_settings)
    a_session(feed_settings, session_id="s-2")

    def _no_key(*a, **k):
        raise llm.MissingKeyError("utility model needs ANTHROPIC_API_KEY")
    monkeypatch.setattr(llm, "utility_complete", _no_key)
    out = feed.sweep(fcon, feed_settings, now=NOW)
    assert out["summarised"] == 0 and out["failed"] == 0
    assert fcon.execute("SELECT COUNT(*) FROM claude_code_sessions").fetchone()[0] == 0


def test_a_sweep_makes_at_most_its_limit_of_model_calls(fcon, feed_settings, fake_llm):
    for i in range(3):
        a_session(feed_settings, session_id=f"s-{i}", mtime=T0 + 3600 + i)
    fake_llm["response"] = "NONE"
    feed.sweep(fcon, feed_settings, now=NOW, limit=2)
    assert len(fake_llm["prompts"]) == 2
    fake_llm["prompts"].clear()
    feed.sweep(fcon, feed_settings, now=NOW + 300, limit=2)
    assert len(fake_llm["prompts"]) == 1                   # the third, next time


def test_the_profile_is_rebuilt_once_per_sweep(fcon, feed_settings, fake_llm,
                                              monkeypatch):
    for i in range(2):
        a_session(feed_settings, session_id=f"s-{i}", mtime=T0 + 3600 + i)
    fake_llm["queue"] = [
        "Alex added a watering schedule.",
        "NEW src=1 importance=5: Alex is building a garden planner app.",
        "Alex added frost warnings.",
        "NEW src=1 importance=5: Alex grows rosemary.",
    ]
    rebuilds = []
    monkeypatch.setattr(feed.summary, "regenerate",
                        lambda con, settings: rebuilds.append(1))
    assert feed.sweep(fcon, feed_settings, now=NOW)["facts_added"] == 2
    assert rebuilds == [1]


# ---- the schedule ----

def test_the_feed_is_off_by_default(tmp_path):
    before = set(threading.enumerate())
    stop = feed.start_scheduler(Settings(data_dir=tmp_path / "data"))
    assert not any(t.name == "claude-code-feed"
                   for t in set(threading.enumerate()) - before)
    stop.set()


def test_a_sweep_holds_the_busy_mark(fcon, feed_settings, monkeypatch):
    seen, done = [], threading.Event()

    def _sweep(con, settings):
        seen.append(busy.snapshot())
        done.set()
    monkeypatch.setattr(feed, "sweep", _sweep)
    stop = feed.start_scheduler(feed_settings)
    try:
        assert done.wait(5)
    finally:
        stop.set()
    assert seen[0] == {"busy": True, "reasons": ["claude-code"]}
