"""Regression tests for the meta-conversation flood — extraction walls flooding review on meta/system
conversations, and overlapping distills multiplying facts — plus the
sibling flood from ordinary builder-process chatter about ANY project.

Four guarantees:
1. A long conversation that is ABOUT the memory system itself yields NO review
   flood: system/AI-tooling self-reference is dropped at extraction, while a
   genuine career fact in the same conversation is kept and not quarantined.
2. A distill for a conversation already in flight is skipped, so overlapping
   runs cannot re-mine the same messages and multiply facts.
3. (First pass) A conversation full of PR/issue/CI mechanics and UI
   styling minutiae yields NO review flood either, while a durable career
   decision in the same conversation is kept and not quarantined.
4. (Reopened) A conversation full of deep TECHNICAL reasoning/diagnosis —
   none of which trips the builder-process keyword regex — yields at most one
   project-thread fact and never surfaces the reasoning itself as a pending
   (or valid) fact. This is the gap the first regex-only fix left open: the fix is
   the extraction prompt's positive two-level rule, not more keywords.
"""

from memory_service import episodic, ledger, mining

# A realistic stretch of THIS kind of chat: mostly the humans and AIs talking
# about the memory system's own machinery, with one real biographical fact
# dropped in the middle. "Sierra" is grounded (present in the transcript);
# "Alex" is the configured user_name and is always allowed by grounding.
_META_CHAT = [
    ("user", "Why do we have 100+ memories stuck in pending review?"),
    ("claude", "The review queue is flooded — the extraction walls fired on this "
               "whole meta conversation about the memory system."),
    ("user", "So the grounding wall and scope wall over-flag whenever we discuss "
             "the memory ledger itself?"),
    ("claude", "Right. Claude Code opened a pull request to fix the distill dedup "
               "and to stop the walls from quarantining system chatter."),
    ("user", "Unrelated to all this: I just started as a staff engineer at Sierra."),
    ("claude", "Congratulations on the Sierra role!"),
    ("user", "We should supersede the stale facts and consolidate the duplicates too."),
]


def _ingest_meta(con, conv_id="meta-1"):
    episodic.ingest(con, "multi-model-chat", conv_id, [
        {"external_id": f"m{i}", "speaker": sp, "content": text,
         "created_at": 1700000000.0 + i * 60}
        for i, (sp, text) in enumerate(_META_CHAT, start=1)])
    return conv_id


def test_meta_conversation_does_not_flood_review(con, settings, fake_llm):
    _ingest_meta(con)
    # The miner proposes three system/meta lines and one real career fact.
    fake_llm["response"] = "\n".join([
        "NEW src=1 importance=2: The memory ledger quarantined over 100 mined facts.",
        "NEW src=3 importance=3: The grounding wall over-flagged the discussion.",
        "NEW src=4 importance=2: The scope wall and the miner need retuning.",
        "NEW src=5 importance=6: Alex started as a staff engineer at Sierra.",
    ])
    res = mining.distill(con, settings, "multi-model-chat", "meta-1", regenerate=False)

    # Three meta lines dropped (not added, not quarantined); one real fact kept.
    assert res == {"added": 1, "quarantined": 0}
    assert ledger.review_queue(con) == []              # nothing flooded review
    valid = ledger.list_facts(con, status="valid")
    assert len(valid) == 1 and "Sierra" in valid[0]["content"]


def test_meta_lines_are_dropped_before_the_grounding_wall(con, settings, fake_llm):
    # A meta line whose proper nouns are ungrounded would otherwise be
    # quarantined by the grounding wall — proving the drop happens first.
    _ingest_meta(con)
    fake_llm["response"] = (
        "NEW src=1 importance=2: The scope wall quarantined a fact about Shelbyville.")
    res = mining.distill(con, settings, "multi-model-chat", "meta-1", regenerate=False)
    assert res == {"added": 0, "quarantined": 0}
    assert ledger.list_facts(con, status="quarantined") == []


# A realistic stretch of ordinary builder-process chatter: PR/issue
# mechanics, review/CI status, and transient UI styling minutiae, with one real
# career decision dropped in the middle.
_BUILDER_CHAT = [
    ("user", "Can you open a PR for issue #142?"),
    ("claude", "Opened pull request #143, merged after code review — CI is green."),
    ("user", "Nice. Also tweak the button color and padding on the settings page."),
    ("claude", "Done — bumped the border-radius and z-index too, pixel-perfect now."),
    ("user", "Separately: I decided to leave Initech and go freelance full-time "
             "starting next month."),
    ("claude", "That's a big move — congrats on going freelance!"),
    ("user", "We hit a null pointer in the CI pipeline; rebased and squash-merged "
             "the fix."),
]


def _ingest_builder_chat(con, conv_id="builder-1"):
    episodic.ingest(con, "multi-model-chat", conv_id, [
        {"external_id": f"b{i}", "speaker": sp, "content": text,
         "created_at": 1700000000.0 + i * 60}
        for i, (sp, text) in enumerate(_BUILDER_CHAT, start=1)])
    return conv_id


def test_builder_process_conversation_does_not_flood_review(con, settings, fake_llm):
    _ingest_builder_chat(con)
    # The miner proposes four process/mechanics lines and one real career fact.
    fake_llm["response"] = "\n".join([
        "NEW src=2 importance=2: Opened pull request #143, merged after code "
        "review with CI green.",
        "NEW src=3 importance=2: Tweaked the button color and padding on the "
        "settings page.",
        "NEW src=4 importance=2: Bumped the border-radius and z-index, "
        "pixel-perfect now.",
        "NEW src=5 importance=7: Alex decided to leave Initech and go "
        "freelance full-time starting next month.",
        "NEW src=7 importance=2: Hit a null pointer in the CI pipeline, "
        "rebased and squash-merged the fix.",
    ])
    res = mining.distill(con, settings, "multi-model-chat", "builder-1", regenerate=False)

    # Four process/mechanics lines dropped (not added, not quarantined); one
    # real career decision kept.
    assert res == {"added": 1, "quarantined": 0}
    assert ledger.review_queue(con) == []               # nothing flooded review
    valid = ledger.list_facts(con, status="valid")
    assert len(valid) == 1 and "freelance" in valid[0]["content"]


# A realistic stretch, fully synthetic: deep technical diagnosis about a
# fictional app's ("Larkspur", invented) cost/latency investigation, none of
# which contains any PR/issue/CI/styling phrase, plus one genuine
# project-thread line worth remembering.
_CACHE_COST_CHAT = [
    ("user", "Larkspur's per-request cost has crept up all week — can we dig into why?"),
    ("claude", "Looking at the request shapes: the provider's response cache keys "
               "on an exact header match, and our client stamps a fresh request id "
               "into the headers every call, so we never actually hit the cache."),
    ("user", "Would that explain the latency spike too, not just the cost?"),
    ("claude", "Yes — root-caused the p95 doubling to the same thing: a cache "
               "miss falls back to a cold fetch, so cost and latency moved "
               "together."),
    ("user", "What are the options to fix it?"),
    ("claude", "A few: strip the request id out of the keyed headers and pass it "
               "as a separate field, widen the cache window, or move the id to a "
               "trailer so the keyed headers stay byte-stable."),
    ("user", "Let's move it to the trailer — smallest diff. Also, PR 42 passed "
             "the full local test suite keyless, so it's ready to open."),
    ("claude", "Opened it — I'll watch cost and latency once it merges."),
    ("user", "Good. Reducing Larkspur's request-cache cost is the main thing I'm "
             "focused on this week."),
]


def _ingest_cache_cost_chat(con, conv_id="cache-cost-1"):
    episodic.ingest(con, "multi-model-chat", conv_id, [
        {"external_id": f"c{i}", "speaker": sp, "content": text,
         "created_at": 1700000000.0 + i * 60}
        for i, (sp, text) in enumerate(_CACHE_COST_CHAT, start=1)])
    return conv_id


def test_technical_diagnosis_yields_at_most_one_project_thread_fact(con, settings, fake_llm):
    """A model following the new prompt proposes exactly one project-thread
    fact and none of the technical reasoning — the outcome the fix is for."""
    _ingest_cache_cost_chat(con)
    fake_llm["response"] = (
        "NEW src=9 importance=4: Alex is working on reducing Larkspur's "
        "request-cache cost.")
    res = mining.distill(con, settings, "multi-model-chat", "cache-cost-1", regenerate=False)

    assert res == {"added": 1, "quarantined": 0}
    assert ledger.review_queue(con) == []                # no review flood
    valid = ledger.list_facts(con, status="valid")
    assert len(valid) == 1
    assert "request-cache cost" in valid[0]["content"]

    # None of the technical reasoning/diagnosis leaked through as a fact —
    # neither valid nor quarantined.
    all_content = " ".join(f["content"] for f in
                           ledger.list_facts(con, status="all"))
    for forbidden in ("exact header match", "root-caused", "cache window",
                      "byte-stable", "PR 42", "cold fetch"):
        assert forbidden not in all_content


def test_mining_prompt_carries_the_two_level_project_rule(con, settings, fake_llm):
    """Locks in the actual instruction the fix depends on — not just its
    effect on a canned response. If this instruction is ever weakened or
    removed, this test catches it even though the pipeline test above
    (which fully controls the fake LLM's output) would not."""
    _ingest_cache_cost_chat(con)
    fake_llm["response"] = "NONE"
    mining.distill(con, settings, "multi-model-chat", "cache-cost-1", regenerate=False)
    prompt = fake_llm["prompts"][0]
    assert "AT MOST ONE" in prompt
    assert "technical reasoning" in prompt
    assert "root-cause analysis" in prompt
    assert "regardless of how detailed" in prompt


def test_distill_skips_when_conversation_already_in_flight(con, settings,
                                                           sample_conversation, fake_llm):
    # Hold the per-conversation lock to simulate an in-flight distill; a second
    # distill must skip immediately and mine nothing — the guard that stops
    # overlapping runs from multiplying facts.
    fake_llm["response"] = "NEW importance=6: Alex works at Initech as a data engineer."
    lock = mining._conversation_lock("multi-model-chat", "chat-1")
    assert lock.acquire(blocking=False)
    try:
        res = mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    finally:
        lock.release()

    assert res == {"added": 0, "quarantined": 0, "skipped_locked": True}
    assert ledger.list_facts(con, status="valid") == []   # watermark untouched

    # Once the in-flight run releases, a fresh distill mines normally.
    res2 = mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    assert res2["added"] == 1


def test_re_mining_the_same_messages_does_not_duplicate_facts(con, settings,
                                                              sample_conversation, fake_llm):
    # The lock guards two runs OVERLAPPING in-process; it cannot guard a run that
    # added facts then died before advancing the watermark, so the next distill
    # re-mines the same messages. The conversation-scoped content_hash guard in
    # add_fact collapses that re-mine instead of multiplying the fact.
    fake_llm["response"] = "NEW importance=6: Alex works at Initech as a data engineer."
    first = mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    assert first["added"] == 1
    assert len(ledger.list_facts(con, status="valid")) == 1

    # Simulate the crash-before-watermark case: rewind the watermark and re-run.
    con.execute("UPDATE conversations SET mined_upto=0 WHERE source_app=? AND external_id=?",
                ("multi-model-chat", "chat-1"))
    con.commit()
    second = mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)

    assert second["added"] == 0
    assert second["deduped"] == 1
    assert len(ledger.list_facts(con, status="valid")) == 1   # no duplicate created


def test_dedupe_is_scoped_to_one_conversation(con, settings):
    # The guard must NOT suppress the same fact arriving from a DIFFERENT
    # conversation (a real freshness signal) or a human save — that stays
    # consolidate.py's advisory job. Only same-conversation re-mines collapse.
    a = ledger.add_fact(con, "Alex brews espresso every morning.", settings,
                         conversation_id=1, dedupe_in_conversation=True)
    same = ledger.add_fact(con, "Alex brews espresso every morning!", settings,
                           conversation_id=1, dedupe_in_conversation=True)
    other = ledger.add_fact(con, "Alex brews espresso every morning.", settings,
                            conversation_id=2, dedupe_in_conversation=True)
    assert same["id"] == a["id"] and same.get("duplicate")
    assert other["id"] != a["id"] and not other.get("duplicate")
