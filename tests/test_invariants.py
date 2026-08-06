"""The six behavioral invariants from docs/API.md — the contract's spine."""

from memory_service import episodic, ledger, mining, recall, summary


def _count(con):
    return con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]


def test_1_append_only_no_automated_delete(con, settings):
    a = ledger.add_fact(con, "Alex plays chess on Sundays.", settings)
    b = ledger.add_fact(con, "Alex plays go on Sundays now.", settings)
    ledger.mark_superseded(con, a["id"], b["id"])
    ledger.quarantine(con, b["id"], "test")
    ledger.dismiss(con, b["id"])
    ledger.approve(con, b["id"])
    assert _count(con) == 2  # every state change kept both rows


def test_2_episodic_never_modified_by_mining(con, settings, sample_conversation, fake_llm):
    fake_llm["response"] = "NEW: Alex works at Initech as a data engineer."
    before = [dict(r) for r in con.execute("SELECT * FROM messages ORDER BY id")]
    mining.distill(con, settings, "multi-model-chat", "chat-1", regenerate=False)
    after = [dict(r) for r in con.execute("SELECT * FROM messages ORDER BY id")]
    assert before == after


def test_3_quarantined_never_in_recall_or_summary(con, settings, fake_llm):
    ledger.add_fact(con, "Alex enjoys hiking in the mountains.", settings)
    q = ledger.add_fact(con, "Alex secretly lives in Atlantis.", settings,
                        quarantine_reason="test contamination")
    hits = recall.recall(con, settings, "Atlantis lives")
    assert all(f["id"] != q["id"] for f in hits)
    fake_llm["response"] = "profile text"
    summary.regenerate(con, settings)
    assert q["id"] not in summary.get(con)["source_fact_ids"]


def test_4_untrusted_origin_always_quarantined(con, settings):
    res = ledger.add_fact(con, "Alex prefers dark roast coffee.", settings,
                          origin_agent="mcp:claude-code")
    assert res["quarantined"]
    row = ledger.get_fact(con, res["id"])
    assert row["quarantined_at"] is not None
    # trusted paths are not gated
    assert not ledger.add_fact(con, "Alex prefers light roast tea.", settings,
                               origin_agent="user")["quarantined"]
    assert not ledger.add_fact(con, "Alex runs marathons.", settings,
                               origin_agent="claude",
                               source_app="multi-model-chat")["quarantined"]
    # #49: an mcp:* origin can NEVER launder itself into canon by declaring a
    # trusted source_app — the write is gated, not the writer.
    assert ledger.add_fact(con, "Alex is secretly a spy.", settings,
                           origin_agent="mcp:claude-code",
                           source_app="multi-model-chat")["quarantined"]
    assert not ledger.is_trusted("mcp:anything", "multi-model-chat", settings)


def test_5_every_fact_has_event_date_and_origin(con, settings):
    ledger.add_fact(con, "Alex speaks fluent Spanish.", settings)
    ledger.add_fact(con, "Alex visited Japan in 2024.", settings,
                    event_date=1710000000.0, origin_agent="mcp:x")
    rows = con.execute("SELECT event_date, origin_agent FROM facts").fetchall()
    assert all(r["event_date"] is not None and r["origin_agent"] for r in rows)


def test_6_summary_claims_trace_to_source_facts(con, settings, fake_llm):
    a = ledger.add_fact(con, "Alex is learning woodworking.", settings)
    fake_llm["response"] = "## Preferences\n- woodworking"
    summary.regenerate(con, settings)
    s = summary.get(con)
    assert s["summary"] == "## Preferences\n- woodworking"
    assert s["source_fact_ids"] == [a["id"]]
    assert s["generated_at"] is not None
    # #110: each traced fact's RAW origin is exposed too, mechanically derived
    # (never a guessed speaker) — see tests/test_summary_provenance.py.
    assert s["provenance"] == [
        {"id": a["id"], "origin_agent": "user", "source": "user", "tag": "direct"}]


def test_ingest_is_idempotent(con, settings, sample_conversation):
    res = episodic.ingest(con, "multi-model-chat", "chat-1", [
        {"external_id": "m1", "speaker": "user", "content": "dup",
         "created_at": 1.0},
        {"external_id": "m3", "speaker": "user", "content": "fresh message",
         "created_at": 1700000120.0},
    ])
    assert res == {"conversation_id": 1, "ingested": 1, "skipped": 1,
                   "attached": 0}


def test_app_version_and_contract_version_are_independent():
    """A release of the app must not silently move the wire contract that
    clients code against, and vice versa. Keeping both here makes a change
    to either one a deliberate edit rather than a side effect."""
    import re

    import memory_service
    from memory_service.config import Settings

    assert re.fullmatch(r"\d+\.\d+\.\d+", memory_service.__version__)
    assert memory_service.__version__ != Settings().contract_version
