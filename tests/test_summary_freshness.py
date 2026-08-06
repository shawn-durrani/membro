"""Freshness + latest-wins: the summary and its MCP surface must let a reader
resolve temporally conflicting status facts, and must never present a stale
profile as if it were timeless.

#22 — the summary prompt carries each entry's event_date (date-only) and an
explicit latest-wins rule for conflicting current-state / active-thread status.
#27 — the MCP memory_summary tool stamps its output with generated_at and warns
that time-sensitive status should be verified with recall.
"""

import datetime

from memory_service import ledger, summary
from memory_service.mcp_server import _format_summary


def test_prompt_carries_event_dates(con, settings, fake_llm):
    """Every ledger entry in the prompt is prefixed with the date it is ABOUT,
    so the model can tell which of two conflicting statuses is current."""
    ed = datetime.datetime(2025, 3, 1).timestamp()
    ledger.add_fact(con, "Alex's job search with a firm is warm.", settings,
                    event_date=ed)
    fake_llm["response"] = "## Identity\n- x"
    summary.regenerate(con, settings)
    p = fake_llm["prompts"][0]
    assert "2025-03-01" in p
    assert "[id · date]" in p  # the prompt explains the bracket


def test_prompt_carries_latest_wins_rule(con, settings, fake_llm):
    """When entries conflict about the same current state, the later date wins
    and older statuses must not be asserted as current."""
    ledger.add_fact(con, "Alex is between roles.", settings)
    fake_llm["response"] = "## Identity\n- x"
    summary.regenerate(con, settings)
    p = fake_llm["prompts"][0]
    assert "LATEST WINS" in p
    assert "later date is the current one" in p
    assert "never assert an older" in p


def test_mcp_summary_is_stamped_with_generated_at():
    """The MCP tool prepends a readable generated_at stamp and a verify-with-
    recall warning ahead of the profile prose (#27)."""
    generated = datetime.datetime(2026, 5, 28, 10, 0, 0,
                                  tzinfo=datetime.timezone.utc).timestamp()
    out = _format_summary({"summary": "## Identity\n- profile body",
                           "generated_at": generated, "source_fact_ids": []})
    assert out.startswith("Profile summary generated_at: 2026-05-28T10:00:00Z")
    assert "verify with recall" in out
    assert out.rstrip().endswith("## Identity\n- profile body")


def test_mcp_summary_tolerates_missing_timestamp():
    """A summary without a usable generated_at still ships, stamped 'unknown'
    rather than crashing the MCP call."""
    out = _format_summary({"summary": "## Identity\n- x", "generated_at": None})
    assert "generated_at: unknown" in out
    assert out.endswith("## Identity\n- x")


def test_mcp_summary_when_empty_has_no_stamp():
    """No summary yet → the plain placeholder, no misleading empty stamp."""
    assert _format_summary({"summary": "", "generated_at": None}) == \
        "No memory summary yet."
    assert _format_summary({}) == "No memory summary yet."
