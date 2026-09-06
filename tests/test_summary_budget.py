"""The summary word budget is honest: enforced by rewriting, never truncation.

The setting must mean what it says — a draft within tolerance ships as-is, an
overshoot gets exactly one rewrite pass, and every failure mode falls back to
the complete-but-verbose draft (fail long, never short: truncation would
silently amputate the LAST sections, the most current ones).

The budget is also a target (#96): the prompt names a range from
`memory_summary_fill` of the budget up to the budget, and a draft under that
floor gets exactly one expansion pass from the same entries, only when they
carry the material. A draft still short after it is kept: fail short, never
invented. Never more than two rewrite passes in a build.
"""

import logging

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


# ---- the floor (#96) ---------------------------------------------------------

SHORT = "## Identity\n- brief"  # 4 words as the counter sees it


def _fill_settings(settings, words=100, fill=0.8):
    """A 100-word budget: the floor is 80, the squeeze trips past 120."""
    return settings.model_copy(update={"memory_summary_words": words,
                                       "memory_summary_fill": fill})


def _seed_ample(con, settings, n=24):
    """Entries that can support the floor: distinct, so the keyless collapse
    keeps every one, and about 450 words in all against a floor of 80."""
    for i in range(n):
        ledger.add_fact(
            con, f"Alex finished workbench project number {i} at Initech on "
                 f"day {i} of the build, with {i * 3} boards cut and planed.",
            settings)


def test_fill_default_is_eighty_percent():
    from memory_service.config import Settings
    assert Settings().memory_summary_fill == 0.8


def test_prompt_names_the_target_range(con, settings, fake_llm):
    """A ceiling alone reads as "stop early" to a model told not to pad: the
    prompt names the floor too, says what the room is for, and no longer
    mentions padding. The ceiling and the per-section cap stay."""
    _seed(con, settings)
    fake_llm["response"] = "## Identity\n- x"
    summary.regenerate(con, settings)
    p = fake_llm["prompts"][0]
    floor = int(settings.memory_summary_words * 0.8)
    assert f"Aim for between {floor} and {settings.memory_summary_words} words" in p
    assert "current state of each thread" in p
    assert "invent nothing" in p
    assert "pad" not in p
    assert f"HARD budget of {settings.memory_summary_words} words total" in p


def test_short_draft_with_ample_entries_gets_one_expansion(con, settings, fake_llm):
    _seed_ample(con, settings)
    s = _fill_settings(settings)
    expanded = "## Identity\n" + "word " * 90  # in range: 92 of 80..100
    fake_llm["queue"] = [SHORT, expanded]
    summary.regenerate(con, s)
    assert len(fake_llm["prompts"]) == 2
    p = fake_llm["prompts"][1]
    assert "The profile below is 4 words, under its target of at least 80 words" in p
    assert "Expand it to between 80 and 100 words" in p
    assert "using ONLY the entries above" in p
    assert "workbench project number 7" in p      # the entries travel again
    assert "LATEST WINS" in p and "PROVENANCE" in p  # with their reading rules
    assert p.rstrip().endswith(SHORT)                # and the draft to expand
    assert summary.get(con)["summary"] == expanded
    assert summary.versions(con)[0]["passes"] == ["expand"]


def test_short_draft_with_thin_entries_is_kept(con, settings, fake_llm):
    """Four words of material cannot fill an 80-word floor: no expansion is
    asked for, because a model told to fill a range it cannot fill from the
    entries reaches for filler."""
    _seed(con, settings)
    s = _fill_settings(settings)
    fake_llm["queue"] = [SHORT]
    fake_llm["fail_when_empty"] = True  # any second call would raise
    summary.regenerate(con, s)
    assert len(fake_llm["prompts"]) == 1
    assert summary.get(con)["summary"] == SHORT
    assert summary.versions(con)[0]["passes"] == []


def test_expansion_still_short_is_kept_and_logged(con, settings, fake_llm, caplog):
    """Fail short, never invented: one expansion, no third attempt, and one
    log line carrying both counts."""
    _seed_ample(con, settings)
    s = _fill_settings(settings)
    still_short = "## Identity\n" + "word " * 40  # 42 words, floor 80
    fake_llm["queue"] = [SHORT, still_short]
    fake_llm["fail_when_empty"] = True
    with caplog.at_level(logging.INFO, logger="memory_service.summary"):
        summary.regenerate(con, s)
    assert len(fake_llm["prompts"]) == 2
    assert summary.get(con)["summary"] == still_short
    assert summary.versions(con)[0]["passes"] == ["expand"]
    lines = [r.getMessage() for r in caplog.records if "floor" in r.getMessage()]
    assert len(lines) == 1 and "42 words" in lines[0] and "80-word floor" in lines[0]


def test_expansion_overshoot_gets_the_squeeze(con, settings, fake_llm):
    """An expansion past the tolerance takes the existing squeeze pass, and a
    build never sends more than two rewrites."""
    _seed_ample(con, settings)
    s = _fill_settings(settings)
    too_long = "## Identity\n" + "word " * 200  # past 120% of 100
    tight = "## Identity\n" + "word " * 90
    fake_llm["queue"] = [SHORT, too_long, tight]
    fake_llm["fail_when_empty"] = True
    summary.regenerate(con, s)
    assert len(fake_llm["prompts"]) == 3
    assert "100-word budget" in fake_llm["prompts"][2]
    assert summary.get(con)["summary"] == tight
    assert summary.versions(con)[0]["passes"] == ["expand", "squeeze"]


def test_expansion_failure_keeps_the_draft(con, settings, fake_llm):
    _seed_ample(con, settings)
    s = _fill_settings(settings)
    fake_llm["queue"] = [SHORT]
    fake_llm["fail_when_empty"] = True  # the expansion call raises
    summary.regenerate(con, s)
    assert len(fake_llm["prompts"]) == 2
    assert summary.get(con)["summary"] == SHORT
    assert summary.versions(con)[0]["passes"] == []


def test_expansion_shorter_than_the_draft_is_discarded(con, settings, fake_llm):
    """A reply shorter than the draft it was asked to expand dropped
    something, or is not a profile at all; the draft stays."""
    _seed_ample(con, settings)
    s = _fill_settings(settings)
    draft = "## Identity\n" + "word " * 20
    fake_llm["queue"] = [draft, "I cannot expand this."]
    summary.regenerate(con, s)
    assert summary.get(con)["summary"] == draft
    assert summary.versions(con)[0]["passes"] == []


def test_zero_fill_turns_the_floor_off(con, settings, fake_llm):
    _seed_ample(con, settings)
    s = _fill_settings(settings, fill=0)
    fake_llm["queue"] = [SHORT]
    fake_llm["fail_when_empty"] = True
    summary.regenerate(con, s)
    assert len(fake_llm["prompts"]) == 1
    p = fake_llm["prompts"][0]
    assert "100 words total" in p  # the ceiling still stands
    assert "Aim for between" not in p
    assert summary.get(con)["summary"] == SHORT


def test_passes_column_is_added_to_an_older_database(settings):
    """A database from before the column existed gains it at startup, and
    its old rows read as unknown rather than as "no passes"."""
    from memory_service import db as db_mod
    settings.data_dir.mkdir(parents=True)
    c = db_mod.connect(settings.db_path)
    c.execute(
        "CREATE TABLE summary_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "generated_at REAL NOT NULL, content TEXT NOT NULL, "
        "source_fact_ids TEXT NOT NULL DEFAULT '[]', "
        "word_count INTEGER NOT NULL DEFAULT 0, word_budget INTEGER, "
        "model TEXT, restored_from INTEGER)")
    c.execute("INSERT INTO summary_versions(generated_at, content) VALUES (1, 'old')")
    c.commit()
    c.close()
    db_mod.init(settings)
    c = db_mod.connect(settings.db_path)
    try:
        assert summary.versions(c)[0]["passes"] is None
    finally:
        c.close()
