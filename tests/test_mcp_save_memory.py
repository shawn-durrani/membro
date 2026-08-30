"""Regression coverage for the engineering-chatter wall on the direct MCP write path.

`save_memory` (mcp_server.py) is the loudest leak the issue names: an
`mcp:*` origin is always quarantined by provenance regardless of content, so
before this fix even blatant PR/issue/CI mechanics or UI styling chatter
landed straight in the human review queue with zero content filtering. This
proves both drop checks (system-meta, builder-process) now run before
`ledger.add_fact`, while a durable preference or meaningful project outcome
still gets through (quarantined, per the existing untrusted-origin gate —
never dropped).
"""

import pytest

from memory_service import ledger, mcp_server, walls


@pytest.fixture(autouse=True)
def _wire_mcp_server(settings, monkeypatch):
    """Point the module-level SETTINGS at this test's temp db, same file the
    `con` fixture already initialized — sqlite (WAL) allows the tool's own
    short-lived connection alongside the fixture's."""
    monkeypatch.setattr(mcp_server, "SETTINGS", settings)


def test_save_memory_drops_system_meta_without_quarantining(con):
    before = ledger.list_facts(con, status="quarantined")
    result = mcp_server.save_memory("The memory ledger quarantined the fact "
                                     "after the grounding wall fired.")
    assert "Not saved" in result
    after = ledger.list_facts(con, status="quarantined")
    assert after == before   # nothing written at all, not even held for review


def test_save_memory_drops_builder_process_noise_without_quarantining(con):
    for content in [
        "Opened pull request #143 for issue #142 and merged after code review.",
        "CI is green now, squash-merged the fix after rebasing on main.",
        "Tweaked the button color, padding, and border-radius on the settings page.",
    ]:
        result = mcp_server.save_memory(content)
        assert "Not saved" in result
    assert ledger.list_facts(con, status="quarantined") == []
    assert ledger.list_facts(con, status="valid") == []


def test_save_memory_still_quarantines_a_durable_outcome_or_preference(con):
    # Process/mechanics content is dropped; a real preference or project
    # outcome from the SAME untrusted origin still reaches review — the
    # existing mcp:* provenance gate is untouched by this fix.
    result = mcp_server.save_memory(
        "Alex decided to leave Initech and go freelance full-time.")
    assert "held for the user's review" in result
    held = ledger.review_queue(con)
    assert len(held) == 1 and "freelance" in held[0]["content"]


def test_save_memory_records_drop_diagnostics_by_reason():
    before = walls.drop_diagnostics()
    mcp_server.save_memory("Merged pull request #7 after CI went green.")
    after = walls.drop_diagnostics()
    assert after.get("builder-process", 0) == before.get("builder-process", 0) + 1


# ---------- search_history honours the owner gate (#81) ----------

def test_search_history_refuses_without_the_owner_token(con, settings,
                                                        monkeypatch):
    """The HTTP route requires the bearer; the MCP path used to require
    nothing. With a token configured and none in the server's environment,
    the tool refuses and touches nothing."""
    monkeypatch.setattr(settings, "auth_token", "owner-tok")
    monkeypatch.delenv("MEMORY_AUTH_TOKEN", raising=False)
    called = []
    monkeypatch.setattr(mcp_server.episodic, "search",
                        lambda *a, **k: called.append(a) or [])
    out = mcp_server.search_history("anything")
    assert "owner-gated" in out and "recall_memory" in out
    assert called == []


def test_search_history_works_with_the_matching_token(con, settings,
                                                      monkeypatch):
    monkeypatch.setattr(settings, "auth_token", "owner-tok")
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "owner-tok")
    monkeypatch.setattr(mcp_server.episodic, "search", lambda *a, **k: [])
    assert mcp_server.search_history("anything") == "No matching messages."


def test_search_history_stays_open_with_no_token_configured(con, settings,
                                                            monkeypatch):
    """An install that never configured a token keeps its open posture,
    matching the HTTP gate."""
    monkeypatch.setattr(settings, "auth_token", None)
    monkeypatch.delenv("MEMORY_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(mcp_server.episodic, "search", lambda *a, **k: [])
    assert mcp_server.search_history("anything") == "No matching messages."
