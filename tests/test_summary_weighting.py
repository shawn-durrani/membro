"""Summary selection — recency × importance on event_date, two pools,
paraphrase collapse. The verified mechanisms behind it are cited in
docs/REFERENCES.md; the hazard it exists to prevent: bulk-mined old facts
evicting current ones, and frequency masquerading as salience."""

import time

from memory_service import db as db_mod
from memory_service import ledger, mining, weighting
from memory_service.embeddings import pack

DAY = 86400.0


def _add(con, content, *, importance=None, event_age_days=0.0,
         embedding=None, now=None):
    now = now or time.time()
    ts = now - event_age_days * DAY
    cur = con.execute(
        "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
        "confidence, importance, content_hash, embedding) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (content, "chat", "test", ts, ts, "high", importance,
         db_mod.content_hash(content), embedding))
    con.commit()
    return cur.lastrowid


def test_score_recent_beats_old_at_equal_importance(con):
    now = time.time()
    recent = {"importance": 5, "event_date": now - 2 * DAY}
    old = {"importance": 5, "event_date": now - 400 * DAY}
    assert weighting.score(recent, now) > weighting.score(old, now)


def test_importance_slows_decay(con):
    """An old life-defining fact outscores an old mundane one (FadeMem)."""
    now = time.time()
    old_major = {"importance": 10, "event_date": now - 300 * DAY}
    old_minor = {"importance": 2, "event_date": now - 300 * DAY}
    fresh_minor = {"importance": 2, "event_date": now - 1 * DAY}
    assert weighting.score(old_major, now) > weighting.score(old_minor, now)
    # ...but recency still wins within the same importance band
    assert weighting.score(fresh_minor, now) > weighting.score(old_minor, now)


def test_durable_recall_check(con):
    """The research's safety rule: a 2-year-old importance-10 fact must still
    reach the summary window, however much recent chatter exists."""
    now = time.time()
    keystone = _add(con, "Test subject got married to Jordan two years ago.",
                    importance=10, event_age_days=730, now=now)
    for i in range(60):
        _add(con, f"Test subject debugged the widget pipeline, attempt {i}.",
             importance=2, event_age_days=1, now=now)
    durable, active = weighting.select_for_summary(con, now=now)
    assert keystone in {f["id"] for f in durable}


def test_bulk_old_facts_do_not_evict_current(con):
    """The backlog-mine hazard: many old-event facts inserted LAST (new ids)
    must not push a current fact out of selection."""
    now = time.time()
    current = _add(con, "Test subject is interviewing at Initech this week.",
                   importance=6, event_age_days=2, now=now)
    for i in range(80):  # bulk mine of a 2025 debugging marathon, inserted after
        _add(con, f"Test subject investigated flaky auth token refresh case {i}.",
             importance=3, event_age_days=450, now=now)
    durable, active = weighting.select_for_summary(con, now=now)
    chosen = {f["id"] for f in durable} | {f["id"] for f in active}
    assert current in chosen


def test_paraphrase_collapse_in_selection(con):
    """Near-identical embeddings collapse to one entry — frequency is not
    salience. The ledger keeps every copy; only selection collapses."""
    now = time.time()
    base = [1.0] + [0.0] * 9
    near = [0.999, 0.001] + [0.0] * 8
    far = [0.0, 1.0] + [0.0] * 8
    a = _add(con, "Test subject prefers dark roast coffee.",
             embedding=pack(base), event_age_days=3, now=now)
    b = _add(con, "Test subject likes their coffee dark roast.",
             embedding=pack(near), event_age_days=2, now=now)
    c = _add(con, "Test subject plays tennis on Saturdays.",
             embedding=pack(far), event_age_days=2, now=now)
    durable, active = weighting.select_for_summary(con, now=now)
    chosen = {f["id"] for f in durable} | {f["id"] for f in active}
    assert c in chosen
    assert not {a, b} <= chosen  # the paraphrase pair collapsed to one
    assert chosen & {a, b}


def test_miner_parses_importance(con, settings, fake_llm, sample_conversation):
    # A scored fact lands valid with its score; an UNSCORED fact that the miner
    # fails to score even on the corrective retry (#69) is quarantined for
    # review, so weighting never has to read its missing score as a neutral 5.
    fake_llm["queue"] = [
        "NEW importance=8: Alex accepted a new role at Initech.\n"
        "NEW: Alex mentioned liking the office coffee.",   # coffee unscored
        "no usable score here",                            # retry also fails
    ]
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    valid = {r["content"]: r["importance"]
             for r in ledger.list_facts(con, status="valid")}
    assert valid == {"Alex accepted a new role at Initech.": 8}
    held = ledger.list_facts(con, status="quarantined")
    assert [f["content"] for f in held] == ["Alex mentioned liking the office coffee."]
    assert "importance" in (held[0]["quarantine_reason"] or "")


def test_miner_importance_with_supersedes(con, settings, fake_llm,
                                          sample_conversation):
    fake_llm["response"] = "NEW importance=7: Alex is a data engineer at Initech."
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    first = con.execute("SELECT id FROM facts").fetchone()[0]
    con.execute("UPDATE conversations SET mined_upto=0")
    con.commit()
    fake_llm["response"] = (
        f"NEW supersedes={first} importance=9: Alex was promoted to lead "
        "data engineer at Initech.")
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    old = con.execute("SELECT invalidated_at, superseded_by FROM facts WHERE id=?",
                      (first,)).fetchone()
    new = con.execute("SELECT importance FROM facts WHERE id=?",
                      (old["superseded_by"],)).fetchone()
    assert old["invalidated_at"] is not None
    assert new["importance"] == 9


def test_distill_chunks_long_conversations(con, settings, fake_llm, monkeypatch):
    """A backlog conversation longer than one context window mines in bounded
    windows, watermark advancing per chunk (resumable if interrupted)."""
    from memory_service import episodic
    monkeypatch.setattr(mining, "CHUNK_MSGS", 2)
    msgs = [{"external_id": f"m{i}", "speaker": "user" if i % 2 else "claude",
             "content": f"message number {i} with enough length to matter",
             "created_at": 1700000000.0 + i} for i in range(7)]
    episodic.ingest(con, "multi-model-chat", "long-chat", msgs, title="Long")
    fake_llm["response"] = "NONE"
    res = mining.distill(con, settings, "multi-model-chat", "long-chat",
                         regenerate=False)
    assert res == {"added": 0, "quarantined": 0}
    # ceil(7/2)=4 windows, but the last holds only a claude message → no
    # user speech → skipped without an LLM call (watermark still advances)
    assert len(fake_llm["prompts"]) == 3
    conv = episodic.get_conversation(con, "multi-model-chat", "long-chat")
    last = con.execute("SELECT MAX(id) FROM messages WHERE conversation_id=?",
                       (conv["id"],)).fetchone()[0]
    assert conv["mined_upto"] == last


def test_distill_chunks_bound_characters_too(con, settings, fake_llm, monkeypatch):
    """A pasted wall of text (one giant message) must not bust the window:
    chunks respect a character budget and oversized messages are truncated."""
    from memory_service import episodic
    monkeypatch.setattr(mining, "CHUNK_CHARS", 500)
    monkeypatch.setattr(mining, "MSG_CHARS", 400)
    msgs = [{"external_id": f"m{i}", "speaker": "user",
             "content": f"pasted document part {i} " + "x" * 300,
             "created_at": 1700000000.0 + i} for i in range(4)]
    episodic.ingest(con, "multi-model-chat", "paste-chat", msgs, title="Paste")
    fake_llm["response"] = "NONE"
    mining.distill(con, settings, "multi-model-chat", "paste-chat",
                   regenerate=False)
    assert len(fake_llm["prompts"]) >= 2  # split, not one giant prompt
    # transcript portion is budget-bounded (the instruction preamble is fixed
    # overhead); a single unsplit prompt would carry ~1300 chars of transcript
    transcripts = [p.split("## Conversation excerpt\n", 1)[1]
                   for p in fake_llm["prompts"]]
    assert all(len(t) <= 500 + len(" … [truncated]") + 60 for t in transcripts)
    conv = episodic.get_conversation(con, "multi-model-chat", "paste-chat")
    last = con.execute("SELECT MAX(id) FROM messages WHERE conversation_id=?",
                       (conv["id"],)).fetchone()[0]
    assert conv["mined_upto"] == last  # still mined to the end


def test_pinned_facts_never_decay(con, settings):
    """Importance 10 = permanent: over a years-long horizon every finite
    half-life reaches zero, so permanence is a tier, not a stretch."""
    from memory_service import weighting
    decade = 10 * 365 * 86400.0
    now = 1700000000.0 + decade
    pinned = {"importance": 10, "event_date": 1700000000.0}
    nine = {"importance": 9, "event_date": 1700000000.0}
    assert weighting.score(pinned, now) == 1.0  # ten years old, undimmed
    assert weighting.score(nine, now) < 0.001   # even a 9 is gone by then


def test_pinned_facts_always_reach_the_profile(con, settings):
    """Pinned facts don't compete for durable-pool slots — permanence must not
    be a contest that a decade of accumulated 8s and 9s can eventually win."""
    from memory_service import ledger, weighting
    old = 1400000000.0  # ancient
    wife = ledger.add_fact(con, "Alex's wife is named Sam.", settings,
                           importance=10, event_date=old)
    # flood the durable pool with newer, high-importance facts
    for i in range(weighting.DURABLE_N + 50):
        ledger.add_fact(con, f"Alex made major life decision number {i}.",
                        settings, importance=9, event_date=1700000000.0 + i)
    durable, _active = weighting.select_for_summary(con, now=1700100000.0)
    assert wife["id"] in {f["id"] for f in durable}  # pinned survives the flood


def test_no_card_falls_between_the_two_pools(con, settings):
    """#55: the durable pool over-selects DURABLE_N × POOL_MARGIN (250)
    candidates so paraphrase collapse can't starve it, then trims back to 200.
    The ~50 the margin trims MUST fall through to the active pool.

    They used to be excluded from it too — `durable_ids` was built from the 250
    candidates while only 200 were folded back — so a ledger holding between
    ~200 and ~550 valid facts had ~50 cards in NEITHER pool, silently absent
    from the profile while the docs promised everything under ~500 was
    represented. The margin exists to survive dedup, not to drop cards.
    """
    from memory_service import ledger, weighting
    now = 1700000000.0
    # 260: past the 250-candidate margin, far short of the 500-card fold — so
    # a correct selection represents EVERY fact. No embeddings in the keyless
    # test path and every content distinct, so collapse is a no-op here.
    ids = {ledger.add_fact(con, f"Alex made decision number {i}.", settings,
                           importance=6, event_date=now - i * DAY)["id"]
           for i in range(260)}
    durable, active = weighting.select_for_summary(con, now=now)
    chosen = {f["id"] for f in durable} | {f["id"] for f in active}
    missing = ids - chosen
    assert not missing, (
        f"{len(missing)} of {len(ids)} facts reached neither pool "
        f"(durable={len(durable)}, active={len(active)})")
    assert len(durable) == weighting.DURABLE_N   # the pool is still bounded…
    assert chosen == ids                         # …and nothing is lost


def test_the_pools_stay_disjoint(con, settings):
    """Durable and active must never both claim a card: the summary prompt
    presents them as two labelled groups, so a duplicate would spend the budget
    twice and read as emphasis the selection never intended."""
    from memory_service import ledger, weighting
    now = 1700000000.0
    for i in range(260):
        ledger.add_fact(con, f"Alex made decision number {i}.", settings,
                        importance=6, event_date=now - i * DAY)
    durable, active = weighting.select_for_summary(con, now=now)
    assert not ({f["id"] for f in durable} & {f["id"] for f in active})


def test_miner_cannot_mint_permanence(con, settings, fake_llm, sample_conversation):
    from memory_service import mining
    fake_llm["response"] = "NEW importance=10: Alex works at Initech."
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    row = con.execute("SELECT importance FROM facts").fetchone()
    assert row["importance"] == 9  # capped: only the owner grants 10
