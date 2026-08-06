"""Consolidation sweep — advisory only, and proposals carry their evidence.

A proposal the human must cross-reference against the ledger by hand is
homework, not a proposal: each one ships the actual facts (newest first) and
an explicit keep_id so the UI can show the cards and offer one-click apply.
The sweep itself never mutates anything.
"""

from memory_service import consolidate, ledger


def test_proposals_carry_facts_and_keep_id(con, settings):
    a = ledger.add_fact(con, "Alex plays chess on Sundays.", settings)
    b = ledger.add_fact(con, "Alex plays chess on Sundays!", settings)  # same hash (normalized)
    ledger.add_fact(con, "Alex dislikes mornings.", settings)  # unrelated
    res = consolidate.run(con, settings)
    assert len(res["proposals"]) == 1
    p = res["proposals"][0]
    assert p["kind"] == "exact-duplicate"
    assert sorted(p["fact_ids"]) == sorted([a["id"], b["id"]])
    assert p["keep_id"] == b["id"]  # equal importance → newest wins
    assert p["facts"][0]["id"] == b["id"]
    assert "chess" in p["facts"][0]["content"]  # evidence inline, no ledger digging


def test_keep_prefers_highest_importance_over_newest(con, settings):
    """Importance stretches a card's half-life — retiring an importance-7
    original to keep a re-minted 5 would make the fact decay FASTER (the
    real-data case: 'wife's name' scored 7 once, re-mints scored 5)."""
    old = ledger.add_fact(con, "Alex's wife is named Sam.", settings, importance=7)
    ledger.add_fact(con, "Alex's wife is named Sam!", settings, importance=5)
    ledger.add_fact(con, "Alex's wife is named Sam?", settings, importance=5)
    p = consolidate.run(con, settings)["proposals"][0]
    assert p["keep_id"] == old["id"]  # the strongest copy, not the newest
    assert p["facts"][0]["importance"] == 7


def test_sweep_never_mutates(con, settings):
    ledger.add_fact(con, "Alex plays chess on Sundays.", settings)
    ledger.add_fact(con, "Alex plays chess on Sundays!", settings)
    before = [dict(r) for r in con.execute("SELECT * FROM facts ORDER BY id")]
    consolidate.run(con, settings)
    after = [dict(r) for r in con.execute("SELECT * FROM facts ORDER BY id")]
    assert before == after  # advisory: proposals only, the human applies


def test_pin_nominations_propose_never_grant(con, settings, fake_llm):
    """The system finds permanent-class facts; only the owner pins. The sweep
    returns nominations for the ids the utility model flags — importance
    itself is untouched until the human clicks."""
    w = ledger.add_fact(con, "Alex's wife is named Sam.", settings, importance=8)
    ledger.add_fact(con, "Alex is job hunting this quarter.", settings, importance=8)
    fake_llm["response"] = f"{w['id']}"
    res = consolidate.run(con, settings)
    pins = [p for p in res["proposals"] if p["kind"] == "pin-nomination"]
    assert len(pins) == 1 and pins[0]["keep_id"] == w["id"]
    assert "Sam" in pins[0]["facts"][0]["content"]  # evidence inline
    # nothing mutated: still 8 until the owner acts
    assert ledger.get_fact(con, w["id"])["importance"] == 8


def test_pin_nominations_degrade_keyless(con, settings, fake_llm):
    """No utility model → no nominations, but the sweep still works."""
    ledger.add_fact(con, "Alex's wife is named Sam.", settings, importance=8)
    ledger.add_fact(con, "Alex plays chess.", settings)
    ledger.add_fact(con, "Alex plays chess!", settings)  # dup pair
    fake_llm["queue"] = []
    fake_llm["fail_when_empty"] = True  # utility call raises (keyless)
    res = consolidate.run(con, settings)
    kinds = [p["kind"] for p in res["proposals"]]
    assert "exact-duplicate" in kinds and "pin-nomination" not in kinds


def test_pin_nominations_ignore_junk_ids(con, settings, fake_llm):
    ledger.add_fact(con, "Alex's wife is named Sam.", settings, importance=8)
    fake_llm["response"] = "999999, NONE, whatever 42"
    res = consolidate.run(con, settings)
    assert not [p for p in res["proposals"] if p["kind"] == "pin-nomination"]
