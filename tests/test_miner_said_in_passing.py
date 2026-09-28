"""What the miner is asked to keep, and how its template is written (#136).

A benchmark run found the miner dropping what people say in passing: the
phone they own, a class they took, their siblings, their age. The
instructions now ask for those, and still leave out one-off numbers. The
template writes its values bare, so the model has no brackets to copy.
"""

import re

import pytest

from memory_service import episodic, ledger, mining
from memory_service.config import Settings

TS = 1740000000.0


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", user_name="Alex",
                    trusted_apps=["multi-model-chat"])


def _instructions():
    return mining._miner_instructions("Alex")


def _examples():
    return [line for line in _instructions().splitlines()
            if line.startswith("NEW ")]


def test_the_template_has_no_bracketed_values():
    text = _instructions()
    assert not re.search(r"=\s*<", text)
    assert "<N>" not in text and "<fact" not in text and "<id>" not in text


def test_every_example_line_reads_as_the_parser_reads_it():
    heads = {}
    for line in _examples():
        m = mining._HEAD_RE.match(line)
        assert m, line
        attrs = m.group("attrs")
        heads[line] = {
            "src": mining._SRC_RE.search(attrs).group(1),
            "importance": mining._IMPORTANCE_RE.search(attrs).group(1),
            "event": (mining._EVENT_RE.search(attrs) or [None, None])[1],
            "supersedes": (mining._SUPERSEDES_RE.search(attrs) or [None, None])[1],
        }
    assert len(heads) == 3
    values = list(heads.values())
    assert all(v["src"] and v["importance"] for v in values)
    assert any(v["supersedes"] == "12" for v in values)
    assert any(v["event"] == "2026-07-13" for v in values)


def test_the_instructions_ask_for_what_is_said_in_passing():
    text = _instructions()
    for phrase in ("WHAT THEY OWN AND USE", "WHAT THEY'VE DONE", "HOME:",
                   "SAID IN PASSING COUNTS", "siblings", "their age"):
        assert phrase in text, phrase
    # The existing entries never decide what kind of fact is worth keeping.
    assert "never to decide what kind of fact is worth keeping" in text


def test_the_instructions_still_leave_out_one_off_numbers():
    text = _instructions()
    assert "what one purchase cost" in text
    assert "keep the fact and drop the number" in text
    assert "mundane detail (a tool preference, a one-off errand)" not in text


def _unrelated_chat(con):
    episodic.ingest(con, "multi-model-chat", "chat-x", [
        {"external_id": "x1", "speaker": "user",
         "content": "Can you suggest a quick weeknight pasta?",
         "created_at": TS},
        {"external_id": "x2", "speaker": "claude",
         "content": "Try aglio e olio.", "created_at": TS + 60},
    ])


def _wrapped_number_example():
    fact = re.search(r"gives '([^']+)'", _instructions()).group(1)
    return f"NEW src=1 importance=4: {fact}"


@pytest.mark.parametrize("example", _examples() + [_wrapped_number_example()])
def test_a_copied_example_is_held(con, settings, fake_llm, example):
    """An example fact copied into a chat that never mentions it is held for
    review by the grounding wall, never written as canon."""
    _unrelated_chat(con)
    fake_llm["queue"] = [example.replace("src=4", "src=1")
                         .replace("src=7", "src=1").replace("src=2", "src=1")]
    fake_llm["fail_when_empty"] = True
    mining.distill(con, settings, "multi-model-chat", "chat-x",
                   regenerate=False)
    assert ledger.list_facts(con, status="valid") == []
    assert len(ledger.list_facts(con, status="quarantined")) == 1
