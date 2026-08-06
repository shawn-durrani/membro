"""#110 — coarse fact provenance survives selection and reaches the summary,
both as a mechanically-derived prompt tag and as structured `/summary`
metadata. Deliberately NOT true speaker attribution: a mined fact spans
possibly several turns and has no single recorded speaker, so the one thing
this suite guards hardest is that a mined (or otherwise ambiguous) fact's raw
origin never turns into an invented "X said" claim — see
docs/API.md ("Ledger" → provenance) and DECISIONS.md.
"""

from memory_service import ledger, mining, summary, weighting


def test_selection_carries_origin_agent_and_source(con, settings):
    """weighting.select_for_summary must expose the raw columns, not just
    content/importance/event_date — the prompt renderer and the /summary
    response both need them (#110)."""
    direct = ledger.add_fact(con, "Alex prefers dark roast coffee.", settings)
    curated = ledger.add_fact(con, "Alex's assistant noted a new hobby.",
                              settings, origin_agent="gpt-5",
                              source_app="multi-model-chat")
    durable, active = weighting.select_for_summary(con)
    by_id = {f["id"]: f for f in durable + active}
    assert by_id[direct["id"]]["origin_agent"] == "user"
    assert by_id[direct["id"]]["source"] == "user"
    assert by_id[curated["id"]]["origin_agent"] == "gpt-5"
    assert by_id[curated["id"]]["source"] == "user"


def test_provenance_tag_is_mechanical_not_guessed():
    """The classifier reads only origin_agent/source — no NLP, no inference."""
    assert summary.provenance_tag({"origin_agent": "user", "source": "user"}) == "direct"
    assert summary.provenance_tag(
        {"origin_agent": "multi-model-chat", "source": "chat"}) == "mined"
    # An unrecognized combination keeps its RAW origin_agent verbatim — never
    # flattened into an invented bucket like "curated".
    assert summary.provenance_tag(
        {"origin_agent": "gpt-5", "source": "user"}) == "gpt-5"
    assert summary.provenance_tag(
        {"origin_agent": "mcp:membro-admin", "source": "user"}) == "mcp:membro-admin"


def test_direct_fact_tagged_direct_in_prompt(con, settings, fake_llm):
    ledger.add_fact(con, "Alex prefers dark roast coffee.", settings)
    fake_llm["response"] = "## Preferences\n- coffee"
    summary.regenerate(con, settings)
    prompt = fake_llm["prompts"][-1]
    assert "· direct] Alex prefers dark roast coffee." in prompt


def test_mined_fact_never_becomes_an_invented_speaker_claim(
        con, settings, fake_llm, sample_conversation):
    """THE negative test: a fact mined from the "multi-model-chat" app carries
    that app slug as origin_agent (mining.py sets origin_agent=source_app) —
    it is NOT a speaker. The prompt must tag it 'mined' and must NEVER print
    the raw app slug next to the fact (which is how a future regression could
    let the model believe "multi-model-chat" — or worse, "the user" — said
    it)."""
    fake_llm["queue"] = [
        "NEW importance=6: Alex accepted a new role at Initech.",
    ]
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    mined = ledger.list_facts(con, status="valid")[0]
    assert mined["origin_agent"] == "multi-model-chat"  # an app slug, not a person
    assert mined["source"] == "chat"

    fake_llm["response"] = "## Work & Projects\n- new role at Initech"
    summary.regenerate(con, settings)
    prompt = fake_llm["prompts"][-1]
    # Isolate the rendered entries (as opposed to the instruction paragraph,
    # which legitimately NAMES "the user said" as the exact phrase to avoid).
    entries = prompt.split("## Durable entries", 1)[1]

    # The fact's line is tagged 'mined' — the raw app slug is NOT rendered at
    # all, so it can never be read by the model as a speaker.
    assert "· mined] Alex accepted a new role at Initech." in entries
    assert "multi-model-chat] Alex accepted" not in entries
    assert "multi-model-chat" not in entries  # the app slug never appears here
    assert "the user said" not in entries.lower()

    # And the instruction (elsewhere in the prompt) explicitly forbids the
    # model from inventing a speaker for a mined entry.
    assert "never write it up as" in prompt.lower()
    assert "no single speaker or turn is recorded" in prompt.lower()


def test_participant_curated_fact_keeps_raw_origin_no_flattening(
        con, settings, fake_llm):
    """A registered app forwarding a model's own curated note (a real,
    trusted, non-mined write) keeps its raw origin_agent — it is not silently
    relabelled 'curated' or anything else invented."""
    ledger.add_fact(con, "Alex's assistant noted a new hobby.", settings,
                    origin_agent="gpt-5", source_app="multi-model-chat")
    fake_llm["response"] = "## Preferences\n- hobby"
    summary.regenerate(con, settings)
    prompt = fake_llm["prompts"][-1]
    assert "· gpt-5] Alex's assistant noted a new hobby." in prompt


def test_summary_response_exposes_structured_provenance(con, settings, fake_llm):
    """Structured metadata (not prose) is the mechanically-checkable surface a
    downstream consumer should actually rely on for attribution (#110)."""
    direct = ledger.add_fact(con, "Alex prefers dark roast coffee.", settings)
    fake_llm["response"] = "## Preferences\n- coffee"
    summary.regenerate(con, settings)
    s = summary.get(con)
    assert s["provenance"] == [
        {"id": direct["id"], "origin_agent": "user", "source": "user",
         "tag": "direct"}]


def test_provenance_survives_restore(con, settings, fake_llm):
    """restore() only has fact ids from history, not full rows — it must
    re-derive provenance from the ledger rather than dropping it."""
    a = ledger.add_fact(con, "Alex prefers dark roast coffee.", settings)
    fake_llm["response"] = "## Preferences\n- coffee (v1)"
    summary.regenerate(con, settings)
    v1 = summary.versions(con)[0]["id"]
    fake_llm["response"] = "## Preferences\n- coffee (v2)"
    summary.regenerate(con, settings)
    assert summary.get(con)["summary"] == "## Preferences\n- coffee (v2)"
    restored = summary.restore(con, v1, settings)
    assert restored["summary"] == "## Preferences\n- coffee (v1)"
    assert restored["provenance"] == [
        {"id": a["id"], "origin_agent": "user", "source": "user", "tag": "direct"}]


def test_empty_summary_has_empty_provenance(con, settings):
    summary.regenerate(con, settings)
    assert summary.get(con)["provenance"] == []
