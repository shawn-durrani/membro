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

import json

import pytest

from memory_service import config, ledger, mcp_server, walls


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


# ---------- search_history honours the owner gate (#81, #117) ----------
#
# The owner token is what the SERVICE runs with: config.json, then
# config.local.json, then .env, which start.sh sources. This process's own
# MEMORY_AUTH_TOKEN is the credential being presented, never the reference.

def _root(path, env_text=None, local=None):
    path.mkdir(parents=True, exist_ok=True)
    if env_text is not None:
        (path / ".env").write_text(env_text)
    if local is not None:
        (path / "config.local.json").write_text(json.dumps(local))
    return path


def _searched(monkeypatch):
    called = []
    monkeypatch.setattr(mcp_server.episodic, "search",
                        lambda *a, **k: called.append(a) or [])
    return called


def test_search_history_refuses_without_the_owner_token(con, tmp_path, monkeypatch):
    """The live shape: the token is in .env only, and the MCP client never
    sources .env. The guest mount leaves the token out on purpose, so it's
    refused, and nothing is searched."""
    monkeypatch.setattr(config, "REPO_ROOT",
                        _root(tmp_path, "MEMORY_AUTH_TOKEN=owner-tok\n"))
    monkeypatch.delenv("MEMORY_AUTH_TOKEN", raising=False)
    called = _searched(monkeypatch)
    out = mcp_server.search_history("anything")
    assert "owner-gated" in out and "recall_memory" in out
    assert called == []


def test_search_history_refuses_a_token_that_only_matches_itself(con, tmp_path,
                                                                 monkeypatch):
    """A wrong token passed with -e used to be compared with itself."""
    monkeypatch.setattr(config, "REPO_ROOT",
                        _root(tmp_path, "MEMORY_AUTH_TOKEN=owner-tok\n"))
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "guessed")
    called = _searched(monkeypatch)
    assert "owner-gated" in mcp_server.search_history("anything")
    assert called == []


def test_search_history_works_with_the_matching_token(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", _root(
        tmp_path, '# owner\nexport MEMORY_AUTH_TOKEN="owner-tok"\n'))
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "owner-tok")
    _searched(monkeypatch)
    assert mcp_server.search_history("anything") == "No matching messages."


def test_search_history_reads_a_token_from_config_local(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT",
                        _root(tmp_path, local={"auth_token": "owner-tok"}))
    monkeypatch.delenv("MEMORY_AUTH_TOKEN", raising=False)
    called = _searched(monkeypatch)
    assert "owner-gated" in mcp_server.search_history("anything")
    assert called == []


def test_search_history_stays_open_with_no_token_configured(con, tmp_path,
                                                            monkeypatch):
    """An install that never configured a token keeps its open posture:
    there's no token for an MCP client to present."""
    monkeypatch.setattr(config, "REPO_ROOT", _root(tmp_path))
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "anything-at-all")
    _searched(monkeypatch)
    assert mcp_server.search_history("anything") == "No matching messages."


def test_configured_token_prefers_env_file_over_config(tmp_path):
    """Same order as the service: start.sh exports .env over the files."""
    both = _root(tmp_path / "both", "MEMORY_AUTH_TOKEN='from-env'\n",
                 local={"auth_token": "from-config"})
    assert config.configured_auth_token(both) == "from-env"
    assert config.configured_auth_token(_root(tmp_path / "none")) is None
