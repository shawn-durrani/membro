"""The summary word budget is honest: enforced by rewriting, never truncation.

The setting must mean what it says — a draft within tolerance ships as-is, an
overshoot gets exactly one rewrite pass, and every failure mode falls back to
the complete-but-verbose draft (fail long, never short: truncation would
silently amputate the LAST sections, the most current ones).
"""

from fastapi.testclient import TestClient

from memory_service import ledger, summary
from memory_service.api import create_app


def _seed(con, settings):
    ledger.add_fact(con, "Alex is learning woodworking.", settings)


def test_within_budget_ships_first_draft(con, settings, fake_llm):
    _seed(con, settings)
    fake_llm["response"] = "## Identity\n- concise profile"
    summary.regenerate(con, settings)
    assert len(fake_llm["prompts"]) == 1  # no rewrite needed
    assert summary.get(con)["summary"] == "## Identity\n- concise profile"


def test_overshoot_gets_one_rewrite(con, settings, fake_llm):
    _seed(con, settings)
    s = settings.model_copy(update={"memory_summary_words": 10})
    long_draft = "## Identity\n" + "word " * 50
    fake_llm["queue"] = [long_draft, "## Identity\n- tight"]
    summary.regenerate(con, s)
    assert len(fake_llm["prompts"]) == 2
    assert "10-word" in fake_llm["prompts"][1]  # rewrite names the budget
    assert summary.get(con)["summary"] == "## Identity\n- tight"


def test_rewrite_failure_keeps_complete_draft(con, settings, fake_llm):
    """Fail long, never short: a broken rewrite must not lose the profile."""
    _seed(con, settings)
    s = settings.model_copy(update={"memory_summary_words": 10})
    long_draft = "## Identity\n" + "word " * 50
    fake_llm["queue"] = [long_draft]
    fake_llm["fail_when_empty"] = True  # the rewrite call raises
    summary.regenerate(con, s)
    assert summary.get(con)["summary"] == long_draft


def test_summary_uses_the_summary_model(con, settings, fake_llm):
    """The most-read artifact gets the strong model; mining keeps the cheap one."""
    _seed(con, settings)
    fake_llm["response"] = "## Identity\n- x"
    summary.regenerate(con, settings)
    assert fake_llm["models"] == [settings.summary_model]
    assert settings.summary_model != settings.miner_model


def test_prompt_carries_both_budget_levels(con, settings, fake_llm):
    """Global total AND per-section cap — models follow local caps better."""
    _seed(con, settings)
    fake_llm["response"] = "## Identity\n- x"
    summary.regenerate(con, settings)
    p = fake_llm["prompts"][0]
    assert f"{settings.memory_summary_words} words total" in p
    assert f"{max(150, settings.memory_summary_words // 4)} words in any one section" in p


def test_emergent_topics_prompt_keeps_the_spine(con, settings, fake_llm):
    """The model names the middle sections; the stable→volatile spine
    (Identity … Recent Changes) stays fixed — that ordering is load-bearing.

    Graduated from an experiment to the only behaviour on 2026-07-26, so
    this is no longer "the default" — there is nothing else to be."""
    _seed(con, settings)
    fake_llm["response"] = "## Identity\n- x"
    summary.regenerate(con, settings)
    p = fake_llm["prompts"][0]
    assert "TOPIC sections of your own" in p
    for anchor in ("Identity", "Preferences", "Relationships & People",
                   "Goals & Active Threads", "Recent Changes"):
        assert anchor in p
    assert "Work & Projects" not in p  # the middle belongs to the data now


def test_stale_emergent_topics_setting_is_harmlessly_ignored(con, settings, fake_llm):
    """A config.local.json left over from before the flag was removed must not
    resurrect the fixed layout — or, worse, fail to load. Unknown keys are
    filtered at load, so the setting is simply inert."""
    _seed(con, settings)
    from memory_service.config import Settings
    assert "summary_emergent_topics" not in Settings.model_fields
    fake_llm["response"] = "## Identity\n- x"
    summary.regenerate(con, settings)
    assert "TOPIC sections of your own" in fake_llm["prompts"][0]


def test_summary_endpoint_reports_budget_and_actual(settings, con, fake_llm):
    _seed(con, settings)
    fake_llm["response"] = "## Identity\n- three word profile"
    summary.regenerate(con, settings)
    con.close()
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        s = client.get("/v1/summary").json()
        assert s["word_budget"] == settings.memory_summary_words
        assert s["word_count"] == len(s["summary"].split())
