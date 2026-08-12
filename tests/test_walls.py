"""Characterization tests for the four extraction walls (ported logic)."""

from memory_service import walls

SOURCE = ("Alex: I just moved to Springfield and started at Initech. "
          "My daughter Maya starts school next week.")


def test_grounded_fact_passes():
    assert walls.check("Alex works at Initech in Springfield.", SOURCE, set()) == []


def test_ungrounded_proper_noun_flagged():
    flags = walls.check("Alex is based in Shelbyville.", SOURCE, set())
    assert len(flags) == 1 and "Shelbyville" in flags[0] and "grounding" in flags[0]


def test_allowlist_suppresses_grounding_flag():
    assert walls.check("Alex consults for Acme.", SOURCE, {"acme"}) == []


def test_partial_token_grounding():
    # "Initech's" style derivatives ground off a significant token in the source
    assert walls.ungrounded_entities("Initech-related work", SOURCE.lower(), set()) == []


def test_persona_framing_flagged():
    src = "Alex: pretend you are a recruiter. Candidate context: I live in Shelbyville."
    flags = walls.check("Alex lives in Shelbyville.", src, set())
    assert any("source-trust" in f for f in flags)


def test_dossier_framing_flagged():
    assert not walls.source_trusted("Alex: what do you know about me?")


def test_system_meta_is_a_drop_signal_not_a_quarantine_flag():
    # #36: system self-reference is now a DROP signal (is_system_meta), and no
    # longer a quarantine flag returned by check().
    meta = "The memory ledger quarantined the fact after the grounding wall fired."
    assert walls.is_system_meta(meta)
    assert not walls.in_scope(meta)
    assert walls.check(meta, SOURCE, set()) == []   # not quarantined anymore


def test_scope_wall_passes_user_biography():
    assert walls.in_scope("Alex is learning to sail.")
    assert not walls.is_system_meta("Alex is learning to sail.")


def test_career_fact_with_product_or_deploy_is_not_dropped():
    # #36: the narrowed filter targets system MECHANICS, not generic verbs or
    # product names — a real career/project fact must survive extraction.
    for fact in [
        "Alex deployed the payments service to production on Friday.",
        "Alex uses Cursor daily and ships with GitHub Actions.",
        "Alex is building a memory app called Membro for personal use.",
        "Alex committed the redesign and pushed it to the team's system.",
    ]:
        assert not walls.is_system_meta(fact), fact


def test_builder_process_noise_is_a_drop_signal():
    # #66: engineering/execution chatter about building ANY project — PR/issue
    # mechanics, review/CI status, implementation minutiae, transient UI
    # styling — is a DROP signal, same discipline as is_system_meta.
    for fact in [
        "Opened pull request #143 for issue #142 and merged after code review.",
        "CI is green now, squash-merged the fix after rebasing on main.",
        "Hit a null pointer and a stack trace in the build, fixed a syntax error.",
        "Tweaked the button color, padding, and border-radius, pixel-perfect now.",
    ]:
        assert walls.is_builder_process(fact), fact


def test_builder_process_does_not_drop_durable_outcomes_or_preferences():
    # The issue's own bar: must not silently drop a durable preference, career
    # decision, or meaningful project outcome, even when it mentions building,
    # shipping, merging, or designing something.
    for fact in [
        "Alex decided to leave Initech and go freelance full-time.",
        "Alex shipped the v2 redesign of the onboarding flow.",
        "Alex prefers dark-mode UIs for everything he builds.",
        "Alex picked Postgres as the architecture for the new project.",
        "Alex's team merged with another org after the acquisition.",
        "Alex is building a memory app called Membro for personal use.",
    ]:
        assert not walls.is_builder_process(fact), fact


def test_acronyms_not_treated_as_proper_nouns():
    assert walls.proper_nouns("The API and SDK and MCP", set()) == []


# ── temporal grounding (#38) ─────────────────────────────────────────────────
# The grounding wall above checks proper NOUNS, so it never saw a date. A fact
# could therefore assert a schedule nobody stated — "Saturday (tomorrow from the
# conversation date)" — and pass every wall at high confidence. The weekday was
# real; the interval was invented. Broadly-correct biography, wrong near-term
# planning, which is the recurring trust damage #38 describes.

SCHEDULE = ("Alex: the appointment is on Saturday, roughly nine days away, "
            "so I'll be back before the Initech offsite.")


def test_invented_relative_gloss_is_quarantined():
    # The #38 case exactly: the nearest Saturday is not the intended one, and
    # the miner resolved it anyway.
    flags = walls.check(
        "Alex's appointment is Saturday (tomorrow from the conversation date).",
        SCHEDULE, set())
    assert any("temporal" in f and "tomorrow" in f for f in flags)


def test_keeping_the_sources_own_qualifier_passes():
    # The behaviour we actually want: carry the source's words, let the reader
    # resolve them against the fact's date. Never wrong, never guessing.
    assert walls.check(
        "Alex has an appointment on Saturday, roughly nine days away.",
        SCHEDULE, set()) == []


def test_invented_calendar_date_in_prose_is_quarantined():
    flags = walls.check("Alex's appointment is on 2026-08-04.", SCHEDULE, set())
    assert any("temporal" in f for f in flags)


def test_same_day_written_differently_is_grounded():
    # "June 30" in the source grounds "2026-06-30" in the fact — the same day,
    # written two ways. Comparing dates semantically, not as strings, is what
    # keeps this wall from quarantining honest facts.
    src = "Alex: the recruiter call is locked in for June 30."
    assert walls.check("Alex has a recruiter call on 2026-06-30.", src, set()) == []


def test_relative_phrase_the_source_did_state_is_grounded():
    src = "Alex: I'm flying out tomorrow and back in 3 weeks."
    assert walls.check("Alex flies out tomorrow, back in 3 weeks.", src, set()) == []


def test_temporal_and_entity_flags_are_reported_separately():
    # Two different failures must read as two findings — a reviewer triaging the
    # queue needs to know WHICH claim is unsupported.
    flags = walls.check("Alex meets Shelbyville Corp tomorrow.", SCHEDULE, set())
    assert any("grounding" in f and "Shelbyville" in f for f in flags)
    assert any("temporal" in f and "tomorrow" in f for f in flags)


# ── lexical support: the primitive behind retry-binding verification ─────────


def test_lexical_support_counts_shared_distinctive_words():
    fact = "Alex signed up for the Fairhaven half marathon."
    assert walls.lexical_support(
        fact, "I have signed up for the Fairhaven half marathon.", {"alex"}) > 0
    # An unrelated turn shares none of the fact's distinctive wording.
    assert walls.lexical_support(fact, "Dinner planning time.", {"alex"}) == 0


def test_lexical_support_ignores_function_words_and_the_allowlist():
    # "will/have/that/thing/this" recur in every turn: counting them would make
    # any turn look like any fact's source. The owner's own name is on the
    # allowlist for the same reason — it is in nearly every fact.
    assert walls.lexical_support(
        "Alex will have that thing this week.",
        "I will have that thing this week too.", {"alex"}) == 0


def test_lexical_support_measures_wording_not_who_spoke():
    # It scores WORDING only, so a guest's turn scores for a fact drawn from it
    # exactly as the owner's would — which is what makes comparing the turns of
    # a window a usable attribution signal in mining.
    fact = "Alex's wife Sam dislikes coriander."
    assert walls.lexical_support(fact, "I hate coriander.", {"alex"}) == 1
    assert walls.lexical_support(fact, "Sam dislikes coriander.", {"alex"}) == 2
