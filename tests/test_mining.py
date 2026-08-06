"""The reflection pass — strict NEW: parsing, walls applied, watermark advances."""

from memory_service import episodic, ledger, mining


def test_distill_mines_and_grounds(con, settings, sample_conversation, fake_llm):
    fake_llm["response"] = (
        "NEW importance=6: Alex works at Initech as a data engineer.\n"
        "NEW importance=3: Alex is based in Shelbyville.\n"  # ungrounded → quarantined
        "Some meta commentary the model added.\n"        # ignored (strict format)
        "NONE"
    )
    res = mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    assert res == {"added": 2, "quarantined": 1}
    held = ledger.review_queue(con)
    assert len(held) == 1 and "Shelbyville" in held[0]["content"]
    assert held[0]["confidence"] == "low"
    clean = ledger.list_facts(con, status="valid")
    assert len(clean) == 1 and "Initech" in clean[0]["content"]
    assert clean[0]["origin_agent"] == "multi-model-chat"
    assert clean[0]["event_date"] == 1700000060.0  # dated to the conversation


def test_distill_supersedes_listed_entry(con, settings, sample_conversation, fake_llm):
    # Target is event-dated BEFORE the superseding conversation — the #120
    # guard refuses the reverse direction.
    old = ledger.add_fact(con, "Alex works at Globex.", settings,
                          event_date=1690000000.0)
    fake_llm["response"] = f"NEW supersedes={old['id']} importance=6: Alex works at Initech now."
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    assert ledger.get_fact(con, old["id"])["invalidated_at"] is not None


def test_supersede_reaches_beyond_recency_window(con, settings, fake_llm):
    """#71: the candidate list the miner sees was ONLY the 250 newest valid
    facts by insertion id, so a target older than that window could never be
    offered for supersession no matter how directly a new fact contradicted
    it. Push the target out of that window with 260 unrelated filler facts,
    then confirm a directly-contradicting new fact still supersedes it."""
    old = ledger.add_fact(con, "Alex works at Globex as a systems architect.",
                          settings, event_date=1690000000.0)  # predates the chat (#120)
    for i in range(260):  # older by content but each insert pushes `old`
        ledger.add_fact(con, f"Filler fact number {i} about something unrelated.",
                        settings)
    assert old["id"] not in {f["id"] for f in ledger.list_facts(con, status="valid", limit=250)}

    from memory_service import episodic
    msgs = [{"external_id": "m1", "speaker": "user",
             "content": "I left Globex and just started at Initech as a data engineer.",
             "created_at": 1700000100.0}]
    episodic.ingest(con, "multi-model-chat", "globex-chat", msgs, title="Job change")
    fake_llm["response"] = (
        f"NEW supersedes={old['id']} importance=6: Alex now works at Initech "
        "as a data engineer, having left Globex.")
    mining.distill(con, settings, "multi-model-chat", "globex-chat", regenerate=False)
    assert ledger.get_fact(con, old["id"])["invalidated_at"] is not None


def test_supersede_survives_many_intervening_mined_facts(con, settings, fake_llm):
    """#71 regression: a fact mined first, then directly contradicted by a
    fact mined much later, is still superseded — independent of how many
    other facts were inserted in between (not just a fixed-N stopgap)."""
    from memory_service import episodic

    msgs1 = [{"external_id": "m1", "speaker": "user",
              "content": "I just started at Wayne Enterprises as a security lead.",
              "created_at": 1700000000.0}]
    episodic.ingest(con, "multi-model-chat", "wayne-chat", msgs1, title="New job")
    fake_llm["response"] = "NEW importance=6: Alex works at Wayne Enterprises as a security lead."
    mining.distill(con, settings, "multi-model-chat", "wayne-chat", regenerate=False)
    old = con.execute("SELECT id FROM facts ORDER BY id LIMIT 1").fetchone()[0]

    for i in range(260):
        ledger.add_fact(con, f"Filler fact number {i} about something unrelated.",
                        settings)
    assert old not in {f["id"] for f in ledger.list_facts(con, status="valid", limit=250)}

    msgs2 = [{"external_id": "m1", "speaker": "user",
              "content": "I left Wayne Enterprises and just joined Stark Industries as CISO.",
              "created_at": 1700005000.0}]
    episodic.ingest(con, "multi-model-chat", "stark-chat", msgs2, title="Job change")
    fake_llm["response"] = (
        f"NEW supersedes={old} importance=7: Alex left Wayne Enterprises and "
        "joined Stark Industries as CISO.")
    mining.distill(con, settings, "multi-model-chat", "stark-chat", regenerate=False)
    assert ledger.get_fact(con, old)["invalidated_at"] is not None


def test_watermark_prevents_remining(con, settings, sample_conversation, fake_llm):
    fake_llm["response"] = "NEW importance=6: Alex works at Initech as a data engineer."
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    res2 = mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    assert res2 == {"added": 0, "quarantined": 0}  # nothing new since watermark


def test_model_only_messages_skip_llm(con, settings, fake_llm):
    episodic.ingest(con, "multi-model-chat", "chat-2", [
        {"external_id": "m1", "speaker": "claude", "content": "assistant monologue",
         "created_at": 1700000000.0}])
    res = mining.distill(con, settings, "multi-model-chat", "chat-2", regenerate=False)
    assert res == {"added": 0, "quarantined": 0}
    assert fake_llm["prompts"] == []  # no user turns → no mining call at all


def test_unknown_conversation_raises(con, settings):
    import pytest
    with pytest.raises(LookupError):
        mining.distill(con, settings, "multi-model-chat", "nope")


# --- Dating by real-world event time (#30) --------------------------------
# A fact defaults to the conversation timestamp, EXCEPT when the source text
# states an explicit calendar date — then it is back-dated to that day. A
# relative/vague phrase, or a date the model invents, is never honoured.
from datetime import datetime, timezone


def _epoch(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp()


def _ingest_one(con, text, ts=1700000060.0):
    from memory_service import episodic
    episodic.ingest(con, "multi-model-chat", "chat-d", [
        {"external_id": "d1", "speaker": "user", "content": text,
         "created_at": ts}])


def test_retrospective_with_explicit_date_is_backdated(con, settings, fake_llm):
    # Conversation happens 2023-11-14 (ts 1700000060) but the fact describes an
    # event the text explicitly pins to 2023-11-01 → dated to the event.
    _ingest_one(con, "Back on 2023-11-01 I signed the lease on the new place.")
    fake_llm["response"] = "NEW src=1 event=2023-11-01 importance=6: Alex signed a lease on 2023-11-01."
    mining.distill(con, settings, "multi-model-chat", "chat-d", regenerate=False)
    fact = ledger.list_facts(con, status="valid")[0]
    assert fact["event_date"] == _epoch(2023, 11, 1)   # back-dated, not 1700000060


def test_explicit_date_without_year_uses_conversation_year(con, settings, fake_llm):
    # "June 30" names no year — the year comes from the conversation (2023),
    # never from the model, so nothing is invented.
    _ingest_one(con, "The recruiter locked our call for June 30 in the evening.")
    fake_llm["response"] = "NEW src=1 event=2023-06-30 importance=5: The recruiter call is locked for June 30."
    mining.distill(con, settings, "multi-model-chat", "chat-d", regenerate=False)
    fact = ledger.list_facts(con, status="valid")[0]
    assert fact["event_date"] == _epoch(2023, 6, 30)


def test_vague_retrospective_stays_at_conversation_time(con, settings, fake_llm):
    # No explicit calendar date in the text → default to created_at, no guess,
    # even though the statement is clearly retrospective.
    _ingest_one(con, "The thread had cooled a couple of weeks ago and stayed quiet.")
    fake_llm["response"] = "NEW importance=3: The recruiter thread had cooled and gone quiet."
    mining.distill(con, settings, "multi-model-chat", "chat-d", regenerate=False)
    fact = ledger.list_facts(con, status="valid")[0]
    assert fact["event_date"] == 1700000060.0          # unchanged


def test_live_present_tense_unchanged(con, settings, fake_llm):
    _ingest_one(con, "I'm starting a new role at Initech as a data engineer.")
    fake_llm["response"] = "NEW importance=6: Alex is a data engineer at Initech."
    mining.distill(con, settings, "multi-model-chat", "chat-d", regenerate=False)
    fact = ledger.list_facts(con, status="valid")[0]
    assert fact["event_date"] == 1700000060.0


def test_hallucinated_date_not_grounded_is_ignored(con, settings, fake_llm):
    # The model emits an event date that appears NOWHERE in the source text.
    # It must be discarded and the fact left at the conversation timestamp.
    _ingest_one(con, "The thread had cooled and gone quiet for a while.")
    fake_llm["response"] = "NEW src=1 event=2023-10-01 importance=3: The recruiter thread had cooled."
    mining.distill(con, settings, "multi-model-chat", "chat-d", regenerate=False)
    fact = ledger.list_facts(con, status="valid")[0]
    assert fact["event_date"] == 1700000060.0          # invented date ignored


# --- Binding a fact to its source message before grounding (#33) ----------
# #32 grounded the model's event= against the WHOLE mined chunk, so a date
# mentioned in any message could ground a fact extracted from a different one.
# Now the extractor tags each fact with src=<N> (a [msg N] label). A VALID
# binding is a hard prerequisite for honouring event=: grounding then reads
# only that one message's text. If src is missing OR invalid, the proposed
# event date is rejected outright and the fact falls back to conversation time
# — never grounded against the latest turn — so the #30/#32 "never guess" holds.


def _ingest_many(con, msgs, conv_id="chat-m"):
    from memory_service import episodic
    episodic.ingest(con, "multi-model-chat", conv_id, [
        {"external_id": f"m{i}", "speaker": sp, "content": text, "created_at": ts}
        for i, (sp, text, ts) in enumerate(msgs, start=1)])
    return conv_id


def test_date_in_other_message_is_rejected(con, settings, fake_llm):
    # The exact #33 hole: June 30 is stated in msg 1, but the fact is bound to
    # msg 2 (which says nothing about a date). The date must NOT ground it even
    # though it is literally present elsewhere in the chunk.
    _ingest_many(con, [
        ("user", "Back on 2023-06-30 we signed the lease on the old place.", 1700000000.0),
        ("user", "The office move is finally sorted.", 1700000100.0)])
    fake_llm["response"] = "NEW src=2 event=2023-06-30 importance=5: Alex's office move is sorted."
    mining.distill(con, settings, "multi-model-chat", "chat-m", regenerate=False)
    fact = ledger.list_facts(con, status="valid")[0]
    assert fact["event_date"] == 1700000100.0          # bound msg's time, not June 30


def test_date_in_bound_message_is_accepted(con, settings, fake_llm):
    # Same two messages; now the fact is correctly bound to msg 1, which states
    # the date — so it grounds and back-dates.
    _ingest_many(con, [
        ("user", "Back on 2023-06-30 we signed the lease on the old place.", 1700000000.0),
        ("user", "The office move is finally sorted.", 1700000100.0)])
    fake_llm["response"] = "NEW src=1 event=2023-06-30 importance=6: Alex signed a lease on 2023-06-30."
    mining.distill(con, settings, "multi-model-chat", "chat-m", regenerate=False)
    fact = ledger.list_facts(con, status="valid")[0]
    assert fact["event_date"] == _epoch(2023, 6, 30)


def test_invalid_src_falls_back_to_conversation_time(con, settings, fake_llm):
    # src=99 names no real message — an assertion of a source that doesn't
    # exist. Even though the proposed date IS present in the latest turn (msg 2),
    # the missing valid binding means the date is rejected outright: no valid
    # binding, no event date, full stop.
    _ingest_many(con, [
        ("user", "We chatted about the roadmap for a while.", 1700000000.0),
        ("user", "The project kickoff is locked for 2023-06-30.", 1700000100.0)])
    fake_llm["response"] = "NEW src=99 event=2023-06-30 importance=5: The project kickoff is set for 2023-06-30."
    mining.distill(con, settings, "multi-model-chat", "chat-m", regenerate=False)
    fact = ledger.list_facts(con, status="valid")[0]
    assert fact["event_date"] == 1700000100.0          # conversation time, not June 30


def test_missing_src_falls_back_to_conversation_time(con, settings, fake_llm):
    # No src at all: a valid binding is required to honour event=. Even with the
    # proposed date sitting in the LATEST turn, it is rejected and the fact stays
    # at conversation time — grounding is never attempted against the latest turn.
    _ingest_many(con, [
        ("user", "We chatted about the roadmap for a while.", 1700000000.0),
        ("user", "The project kickoff is locked for 2023-06-30.", 1700000100.0)])
    fake_llm["response"] = "NEW event=2023-06-30 importance=5: The project kickoff is set for 2023-06-30."
    mining.distill(con, settings, "multi-model-chat", "chat-m", regenerate=False)
    fact = ledger.list_facts(con, status="valid")[0]
    assert fact["event_date"] == 1700000100.0          # conversation time, not June 30


def test_bound_message_sets_source_message_id(con, settings, fake_llm):
    # Binding also fixes provenance: the stored source_message_id is the bound
    # turn, not always the last one in the window.
    _ingest_many(con, [
        ("user", "Back on 2023-06-30 we signed the lease on the old place.", 1700000000.0),
        ("user", "The office move is finally sorted.", 1700000100.0)])
    fake_llm["response"] = "NEW src=1 importance=6: Alex signed a lease on the old place."
    mining.distill(con, settings, "multi-model-chat", "chat-m", regenerate=False)
    fact = ledger.list_facts(con, status="valid")[0]
    conv = episodic.get_conversation(con, "multi-model-chat", "chat-m")
    first = episodic.messages_after(con, conv["id"], 0)[0]
    assert fact["source_message_id"] == first["id"]


def test_grounded_event_date_helper():
    # Direct unit coverage of the grounding gate.
    ref = 1700000060.0                                  # 2023-11-14 UTC
    assert mining._grounded_event_date(
        "2026-06-30", "we met on June 30, 2026 to sign", ref) == _epoch(2026, 6, 30)
    assert mining._grounded_event_date(
        "2023-06-30", "call locked for June 30", ref) == _epoch(2023, 6, 30)
    assert mining._grounded_event_date(
        "2023-07-13", "on the 13th of July we spoke", ref) == _epoch(2023, 7, 13)
    # Ungrounded / relative / malformed → None (caller defaults to conv time)
    assert mining._grounded_event_date("2023-10-01", "cooled last week", ref) is None
    assert mining._grounded_event_date(None, "June 30 is the date", ref) is None
    assert mining._grounded_event_date("not-a-date", "June 30", ref) is None
    # Text names a DIFFERENT year than the model → not grounded
    assert mining._grounded_event_date(
        "2026-06-30", "we met on June 30, 2025", ref) is None


def test_invented_relative_gloss_is_held_for_review(con, settings, fake_llm):
    """#38 end-to-end: the source states a relative schedule; the miner resolves
    it to the wrong day and states that resolution as fact. The event_date was
    already safe (a relative phrase can never ground one), but the fact's PROSE
    asserted a schedule nobody stated — and no wall looked at prose dates, so it
    landed valid at high confidence. It is now quarantined for review."""
    _ingest_one(con, "The appointment is on Saturday, roughly nine days away.")
    fake_llm["response"] = ("NEW src=1 importance=4: Alex has an appointment on "
                            "Saturday (tomorrow from the conversation date).")
    res = mining.distill(con, settings, "multi-model-chat", "chat-d",
                         regenerate=False)
    # Written AND held: `added` counts every fact written, `quarantined` is the
    # subset held for review. Never silently dropped, never silently trusted.
    assert res["added"] == 1 and res["quarantined"] == 1
    assert ledger.list_facts(con, status="valid") == []   # so it reaches no recall
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "temporal" in (held["quarantine_reason"] or "")
    assert held["confidence"] == "low"


def test_source_qualifier_kept_verbatim_passes_clean(con, settings, fake_llm):
    """The behaviour the wall is steering toward: carry the source's own words.
    The reader resolves them against the fact's date — never wrong, no guess."""
    _ingest_one(con, "The appointment is on Saturday, roughly nine days away.")
    fake_llm["response"] = ("NEW src=1 importance=4: Alex has an appointment on "
                            "Saturday, roughly nine days away.")
    res = mining.distill(con, settings, "multi-model-chat", "chat-d",
                         regenerate=False)
    assert res["added"] == 1 and res["quarantined"] == 0
    fact = ledger.list_facts(con, status="valid")[0]
    assert fact["event_date"] == 1700000060.0   # conversation time, not resolved


# --- importance is REQUIRED: retry once, then quarantine (#69) -------------
# The miner (a smaller utility model) began dropping the importance= tag after
# the output grammar grew event= and src= (2026-07-15). The parser used to
# store the omission as NULL, which summary selection reads as a neutral 5 — so
# genuinely important recent facts silently lost their profile slot to older,
# explicitly scored ones. A missing or malformed importance= now triggers ONE
# corrective re-ask for that fact's score; only a genuine SECOND failure
# quarantines. This keeps the high first-pass omission rate from flooding
# review while never storing an unscored fact as trusted canon.


def test_missing_importance_retry_supplies_it_and_fact_lands_valid(
        con, settings, sample_conversation, fake_llm):
    # First pass omits importance=; the corrective retry reissues the fact WITH
    # a score → it lands valid at that score, no quarantine.
    fake_llm["queue"] = [
        "NEW: Alex accepted a new role at Initech.",            # 1st: no score
        "NEW importance=8: Alex accepted a new role at Initech.",  # retry: scored
    ]
    res = mining.distill(con, settings, "multi-model-chat", "chat-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 0}
    valid = ledger.list_facts(con, status="valid")
    assert len(valid) == 1 and valid[0]["importance"] == 8
    assert ledger.list_facts(con, status="quarantined") == []
    assert fake_llm["queue"] == []   # exactly two calls: extraction + one retry


def test_missing_importance_bare_retry_score_also_accepted(
        con, settings, sample_conversation, fake_llm):
    # The retry may answer in the terse `importance=<N>` form the prompt asks
    # for, not a full NEW line — that is accepted just the same.
    fake_llm["queue"] = [
        "NEW: Alex accepted a new role at Initech.",
        "importance=6",
    ]
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    valid = ledger.list_facts(con, status="valid")
    assert len(valid) == 1 and valid[0]["importance"] == 6


def test_missing_importance_quarantined_when_retry_also_fails(
        con, settings, sample_conversation, fake_llm):
    # First pass omits importance= AND the corrective retry fails to supply it →
    # the backstop: written and held for review, never silently trusted.
    fake_llm["queue"] = [
        "NEW: Alex accepted a new role at Initech.",   # 1st: no score
        "I'm not sure how to score that.",             # retry: still no score
    ]
    res = mining.distill(con, settings, "multi-model-chat", "chat-1",
                         regenerate=False)
    assert res["added"] == 1 and res["quarantined"] == 1
    assert ledger.list_facts(con, status="valid") == []   # reaches no summary
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "importance" in (held["quarantine_reason"] or "")
    assert "retry" in (held["quarantine_reason"] or "")
    assert held["confidence"] == "low"


def test_malformed_importance_quarantined_when_retry_also_fails(
        con, settings, sample_conversation, fake_llm):
    # A non-numeric value the importance= regex won't match is treated exactly
    # like a missing tag: retried once, then quarantined if the retry is no
    # better — never silently stored as NULL.
    fake_llm["queue"] = [
        "NEW importance=high: Alex accepted a new role at Initech.",
        "importance=very high",   # retry: still no usable number
    ]
    res = mining.distill(con, settings, "multi-model-chat", "chat-1",
                         regenerate=False)
    assert res["added"] == 1 and res["quarantined"] == 1
    assert ledger.list_facts(con, status="valid") == []
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "importance" in (held["quarantine_reason"] or "")


def test_backdated_fact_cannot_supersede_newer(con, settings, fake_llm):
    """#120: mining old history (imported exports, late re-mines) must never
    invalidate a newer fact. The old-dated fact still lands, event-dated to
    its conversation; only the supersession is refused."""
    from memory_service import episodic
    current = ledger.add_fact(con, "Alex works at Initech.", settings)
    msgs = [{"external_id": "m1", "speaker": "user",
             "content": "I'm settling into my new job at Globex.",
             "created_at": 1700000100.0}]
    episodic.ingest(con, "multi-model-chat", "old-remine-chat", msgs,
                    title="Old chat")
    fake_llm["response"] = (
        f"NEW supersedes={current['id']} importance=6: Alex works at Globex.")
    res = mining.distill(con, settings, "multi-model-chat", "old-remine-chat",
                         regenerate=False)
    assert res["added"] == 1
    assert res["refused_supersede"] == 1
    valid = ledger.list_facts(con, status="valid")
    assert any(f["id"] == current["id"] for f in valid)  # newer fact survives
    mined = [f for f in valid if "Globex" in f["content"]]
    assert mined and mined[0]["event_date"] == 1700000100.0


def test_quarantined_fact_defers_supersession(con, settings, fake_llm):
    """#120: an untrusted-origin mined fact is quarantined by the write gate —
    and a held-for-review fact must not alter canon. Its supersession is
    deferred (surfaced in the result), never auto-applied."""
    from memory_service import episodic
    old = ledger.add_fact(con, "Alex works at Globex.", settings,
                          event_date=1690000000.0)
    msgs = [{"external_id": "m1", "speaker": "user",
             "content": "I left Globex and now work at Initech.",
             "created_at": 1700000100.0}]  # NEWER than the target — direction ok
    episodic.ingest(con, "claude.ai", "imported-chat", msgs, title="Import")
    fake_llm["response"] = (
        f"NEW supersedes={old['id']} importance=6: Alex works at Initech.")
    res = mining.distill(con, settings, "claude.ai", "imported-chat",
                         regenerate=False)
    assert res["quarantined"] == 1
    assert res["deferred_supersede"] == 1
    assert "refused_supersede" not in res
    valid_ids = {f["id"] for f in ledger.list_facts(con, status="valid")}
    assert old["id"] in valid_ids           # canon untouched by quarantine
    held = ledger.list_facts(con, status="quarantined")
    assert any("Initech" in f["content"] for f in held)


def test_same_day_supersession_passes(con, settings, fake_llm):
    """Day-granularity guard: a same-day correction still supersedes."""
    from memory_service import episodic
    old = ledger.add_fact(con, "Alex works at Globex.", settings,
                          event_date=1700000000.0)
    msgs = [{"external_id": "m1", "speaker": "user",
             "content": "Correction — I actually joined Initech, not Globex.",
             "created_at": 1700000100.0}]
    episodic.ingest(con, "multi-model-chat", "same-day-chat", msgs, title="Fix")
    fake_llm["response"] = (
        f"NEW supersedes={old['id']} importance=6: Alex works at Initech.")
    res = mining.distill(con, settings, "multi-model-chat", "same-day-chat",
                         regenerate=False)
    assert "refused_supersede" not in res
    assert old["id"] not in {f["id"] for f in ledger.list_facts(con, status="valid")}
