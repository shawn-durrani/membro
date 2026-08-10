"""Guest speakers in ingest (#31): attribution and quarantine-by-default.

Multi-human voice sessions attribute turns to named humans beside the owner
(`guest:<name>`) or mark them unidentified (`guest:unknown`). Facts mined
from such speech are written but held for review with the speaker named in
the reason — the posture `mcp:*` writes already get — and a guest's
first-person statement lands in the owner's ledger in third person, never as
first-person biography about the owner. Unrecognised speaker classes are
untrusted by default, so a newer client never silently gains trust.
"""

from memory_service import episodic, ledger, mining, walls


def _ingest(con, msgs, conv="room-1"):
    episodic.ingest(con, "multi-model-chat", conv, [
        {"external_id": f"m{i}", "speaker": sp, "content": text,
         "created_at": 1700000000.0 + i * 60}
        for i, (sp, text) in enumerate(msgs, start=1)], title="Kitchen chat")
    return conv


# ── speaker classification (walls) ──────────────────────────────────────────


def test_speaker_classes():
    assert walls.speaker_class("user") == "owner"
    assert walls.speaker_class("claude") == "model"
    assert walls.speaker_class("gpt-5") == "model"
    assert walls.speaker_class("guest:Sam") == "guest"
    assert walls.speaker_class("Guest:Sam") == "guest"          # prefix case-blind
    assert walls.speaker_class("guest:unknown") == "guest-unknown"
    assert walls.speaker_class("guest:Unknown") == "guest-unknown"
    assert walls.speaker_class("guest:") == "guest-unknown"     # nameless guest
    assert walls.speaker_class("agent:scribe") == "unrecognised"
    assert walls.speaker_class("tool:web-search") == "unrecognised"
    assert walls.speaker_class("") == "unrecognised"


def test_guest_name_extraction():
    assert walls.guest_name("guest:Sam") == "Sam"
    assert walls.guest_name("guest: Sam ") == "Sam"
    assert walls.guest_name("guest:unknown") is None
    assert walls.guest_name("user") is None
    assert walls.guest_name("claude") is None


def test_speaker_trust_flags():
    assert walls.speaker_trust_flag("user") is None      # the owner: unchanged
    assert walls.speaker_trust_flag("claude") is None    # model seat: unchanged
    named = walls.speaker_trust_flag("guest:Sam")
    assert "guest" in named and "Sam" in named
    unknown = walls.speaker_trust_flag("guest:unknown")
    assert "unidentified" in unknown
    other = walls.speaker_trust_flag("agent:scribe")
    assert "unrecognised" in other and "agent:scribe" in other


# ── transcript rendering and prompt guidance ────────────────────────────────


def test_transcript_labels_guests():
    msgs = [
        {"speaker": "user", "content": "Hello there."},
        {"speaker": "guest:Sam", "content": "Hi, it's me."},
        {"speaker": "guest:unknown", "content": "And someone else."},
        {"speaker": "claude", "content": "Welcome, everyone."},
    ]
    text = mining._transcript(msgs, "Alex")
    assert "[msg 1] Alex:" in text
    assert "[msg 2] Sam (guest):" in text
    assert "[msg 3] Unidentified speaker (guest):" in text
    assert "[msg 4] claude:" in text


def test_prompt_guest_guidance_only_when_guests_present(
        con, settings, sample_conversation, fake_llm):
    _ingest(con, [
        ("user", "Sam is here with me."),
        ("guest:Sam", "I hate coriander."),
    ], conv="room-p")
    fake_llm["response"] = "NONE"
    mining.distill(con, settings, "multi-model-chat", "room-p", regenerate=False)
    assert "GUEST SPEAKERS" in fake_llm["prompts"][0]
    # The everyday owner-only path carries no guest guidance at all.
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    assert "GUEST SPEAKERS" not in fake_llm["prompts"][1]


# ── quarantine-by-default for guest-derived facts ───────────────────────────


def test_guest_first_person_fact_quarantined_with_guest_name(
        con, settings, fake_llm):
    # A guest's "I hate coriander" is a fact about the GUEST, phrased in third
    # person into the owner's ledger — and held for review, never canon on
    # sight, with the guest named in the reason.
    _ingest(con, [
        ("user", "Sam and I are planning the Fairhaven trip."),
        ("guest:Sam", "I hate coriander, please leave it out of everything."),
    ])
    fake_llm["response"] = (
        "NEW src=2 importance=4: Alex's wife Sam dislikes coriander.")
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 1}   # written AND held
    assert ledger.list_facts(con, status="valid") == []
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "guest" in held["quarantine_reason"]
    assert "Sam" in held["quarantine_reason"]
    assert held["confidence"] == "low"
    # The review flow is the same as for any held fact: an owner approval
    # makes it canon, third-person phrasing and all.
    ledger.approve(con, held["id"])
    assert any(f["id"] == held["id"]
               for f in ledger.list_facts(con, status="valid"))


def test_owner_speech_unchanged_in_mixed_session(con, settings, fake_llm):
    # The owner's own turns keep their existing trust even with a guest in the
    # room: only the guest-bound fact is held.
    _ingest(con, [
        ("user", "I have signed up for the Fairhaven half marathon."),
        ("guest:Sam", "And I am doing the ten kilometre run."),
    ])
    fake_llm["response"] = (
        "NEW src=1 importance=5: Alex signed up for the Fairhaven half marathon.\n"
        "NEW src=2 importance=4: Alex's wife Sam is doing the ten kilometre run.")
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 2, "quarantined": 1}
    valid = ledger.list_facts(con, status="valid")
    assert len(valid) == 1 and "marathon" in valid[0]["content"]
    held = ledger.list_facts(con, status="quarantined")
    assert len(held) == 1 and "Sam" in held[0]["content"]


def test_fact_about_owner_from_guest_speech_still_quarantines(
        con, settings, fake_llm):
    # Guest-provenance is about WHO SPOKE, not who the fact mentions: a claim
    # ABOUT the owner asserted by a guest still quarantines by default.
    _ingest(con, [
        ("user", "Let's sort out dinner."),
        ("guest:Sam", "Remember Alex is allergic to shellfish."),
    ])
    fake_llm["response"] = "NEW src=2 importance=7: Alex is allergic to shellfish."
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 1}
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "Sam" in held["quarantine_reason"]


def test_guest_unknown_quarantines_unconditionally(con, settings, fake_llm):
    _ingest(con, [
        ("user", "Who else is in the room?"),
        ("guest:unknown", "I grew up in Fairhaven."),
    ])
    fake_llm["response"] = (
        "NEW src=2 importance=3: A guest of Alex's grew up in Fairhaven.")
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 1}
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "unidentified" in held["quarantine_reason"]


def test_unrecognised_speaker_class_is_untrusted(con, settings, fake_llm):
    # Fail safe: a speaker class this build does not know behaves as
    # untrusted, so a newer client never silently gains trust.
    _ingest(con, [
        ("user", "Planning the week."),
        ("agent:scribe", "Alex works at Initech."),
    ])
    fake_llm["response"] = "NEW src=2 importance=5: Alex works at Initech."
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 1}
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "unrecognised" in held["quarantine_reason"]
    assert "agent:scribe" in held["quarantine_reason"]


def test_unbound_fact_in_guest_session_quarantines(con, settings, fake_llm):
    # Fail safe for a missing src= binding: in a session with guest turns, a
    # fact that names no source turn cannot be attributed to the owner, so it
    # is held. (In an owner-only session an unbound fact stays valid, as
    # test_missing_src_falls_back_to_conversation_time proves.) With no queue,
    # the #35 corrective retry sees the same unusable response and the
    # fail-safe backstop is what this test pins down.
    _ingest(con, [
        ("user", "We are planning this week's meals."),
        ("guest:Sam", "I hate coriander."),
    ])
    fake_llm["response"] = "NEW importance=4: Alex's wife Sam dislikes coriander."
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 1}
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "src=" in held["quarantine_reason"]
    assert "guest" in held["quarantine_reason"]


def test_guest_only_chunk_still_mined(con, settings, fake_llm):
    # The owner can be silent in a window; a guest's speech still mines, and
    # everything it yields is held for review.
    _ingest(con, [
        ("guest:Sam", "I have started a new job at Globex."),
        ("claude", "Congratulations on the Globex role!"),
    ])
    fake_llm["response"] = (
        "NEW src=1 importance=5: Sam (a guest) has started a new job at Globex.")
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 1}
    assert fake_llm["prompts"]      # the miner ran
    assert ledger.list_facts(con, status="valid") == []


def test_unrecognised_only_chunk_skips_mining(con, settings, fake_llm):
    # A window of only unrecognised-class and model turns is not known to be
    # human speech at all — no mining call, exactly like model-only windows.
    _ingest(con, [
        ("agent:scribe", "Meeting notes: budget approved."),
        ("claude", "Noted."),
    ])
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 0, "quarantined": 0}
    assert fake_llm["prompts"] == []


# ── corrective retry for missing src= in guest sessions (#35) ───────────────


def test_src_retry_binds_owner_fact_to_owner_turn(con, settings, fake_llm):
    # The miner omits src= on a fact the owner stated. ONE batched corrective
    # retry supplies the binding, so the fact lands as canon instead of
    # burning a review-queue row on the generic fail-safe reason.
    _ingest(con, [
        ("user", "I have signed up for the Fairhaven half marathon."),
        ("guest:Sam", "I will cheer from the finish line."),
    ])
    fake_llm["queue"] = [
        "NEW importance=5: Alex signed up for the Fairhaven half marathon.",
        "FACT 1: src=1",
    ]
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 0}
    valid = ledger.list_facts(con, status="valid")
    assert len(valid) == 1 and "marathon" in valid[0]["content"]
    # The retry prompt carried the fact list and the labelled excerpt.
    assert "FACT 1" in fake_llm["prompts"][1]
    assert "[msg 1]" in fake_llm["prompts"][1]


def test_src_retry_binding_to_guest_turn_still_holds(con, settings, fake_llm):
    # The retry restores ATTRIBUTION, never trust: a fact the retry binds to
    # a guest's turn holds with the guest named in the reason, exactly as a
    # first-pass binding to that turn would.
    _ingest(con, [
        ("user", "Dinner planning time."),
        ("guest:Sam", "I hate coriander."),
    ])
    fake_llm["queue"] = [
        "NEW importance=4: Alex's wife Sam dislikes coriander.",
        "FACT 1: src=2",
    ]
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 1}
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "Sam" in held["quarantine_reason"]
    assert "no valid src=" not in held["quarantine_reason"]


def test_src_retry_second_failure_keeps_fail_safe_hold(con, settings, fake_llm):
    # A genuine second failure — the retry names a turn the window does not
    # contain — falls through to the existing fail-safe hold, never silent
    # trust. src=none (honest cross-turn synthesis) behaves identically.
    _ingest(con, [
        ("user", "Dinner planning time."),
        ("guest:Sam", "I hate coriander."),
    ])
    fake_llm["queue"] = [
        "NEW importance=4: Alex's wife Sam dislikes coriander.",
        "FACT 1: src=99",
    ]
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 1}
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "no valid src=" in held["quarantine_reason"]
    assert "retry" in held["quarantine_reason"]


def test_src_retry_is_one_batched_call(con, settings, fake_llm):
    # Two unbound facts cost ONE retry call, not one per fact: the binding
    # can't be re-asked without resending the excerpt, so the repair batches.
    _ingest(con, [
        ("user", "I have signed up for the Fairhaven half marathon."),
        ("guest:Sam", "And I am doing the ten kilometre run."),
    ])
    fake_llm["queue"] = [
        "NEW importance=5: Alex signed up for the Fairhaven half marathon.\n"
        "NEW importance=4: Alex's wife Sam is doing the ten kilometre run.",
        "FACT 1: src=1\nFACT 2: src=2",
    ]
    res = mining.distill(con, settings, "multi-model-chat", "room-1",
                         regenerate=False)
    assert res == {"added": 2, "quarantined": 1}
    assert len(fake_llm["prompts"]) == 2   # mine + one batched retry
    valid = ledger.list_facts(con, status="valid")
    assert len(valid) == 1 and "marathon" in valid[0]["content"]
    held = ledger.list_facts(con, status="quarantined")[0]
    assert "Sam" in held["quarantine_reason"]


def test_no_src_retry_in_owner_only_window(con, settings, fake_llm,
                                           sample_conversation):
    # Owner-only windows never pay for the retry: an unbound fact there is
    # attributable by definition (every human turn is the owner's), so the
    # miner makes exactly one call and the fact lands valid as it always has.
    fake_llm["response"] = "NEW importance=5: Alex moved to Springfield."
    res = mining.distill(con, settings, "multi-model-chat", "chat-1",
                         regenerate=False)
    assert res == {"added": 1, "quarantined": 0}
    assert len(fake_llm["prompts"]) == 1


# ── ingest accepts the new class (additive contract) ────────────────────────


def test_ingest_stores_guest_speakers_verbatim(con, settings):
    _ingest(con, [
        ("user", "Dinner planning time."),
        ("guest:Sam", "No coriander for me."),
        ("guest:unknown", "And someone else spoke."),
    ], conv="room-v")
    conv = episodic.get_conversation(con, "multi-model-chat", "room-v")
    rows = episodic.messages_after(con, conv["id"], 0)
    assert [r["speaker"] for r in rows] == ["user", "guest:Sam", "guest:unknown"]
    # Idempotent like any other ingest: a re-send inserts nothing new.
    res = episodic.ingest(con, "multi-model-chat", "room-v", [
        {"external_id": "m2", "speaker": "guest:Sam",
         "content": "No coriander for me.", "created_at": 1700000120.0}])
    assert res["ingested"] == 0 and res["skipped"] == 1


def test_http_ingest_accepts_guest_speakers(settings, fake_llm):
    from fastapi.testclient import TestClient

    from memory_service.api import create_app
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.post("/v1/ingest", json={
        "source_app": "multi-model-chat", "conversation_id": "room-h",
        "title": "Kitchen chat",
        "messages": [
            {"external_id": "m1", "speaker": "user",
             "content": "Dinner planning time.",
             "created_at": "2026-08-08T10:00:00+10:00"},
            {"external_id": "m2", "speaker": "guest:Sam",
             "content": "No coriander for me.",
             "created_at": "2026-08-08T10:01:00+10:00"},
            {"external_id": "m3", "speaker": "guest:unknown",
             "content": "And someone else spoke.",
             "created_at": "2026-08-08T10:02:00+10:00"},
        ]})
    assert r.status_code == 200
    assert r.json() == {"ingested": 3, "skipped": 0, "attached": 0}
