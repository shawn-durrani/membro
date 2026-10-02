"""scripts/raise_approved_confidence.py, the one-off repair for #195.

Until #195 approving a fact held by the write gate or the miner's checks
kept the low mark the hold gave it. The owner's rule for the facts approved
before the fix: raise only those membro's log shows were approved, and never
edited by hand, that are still current, not held, and low because a hold
made them so. Everything here runs on a throwaway ledger and a made-up log,
with the synthetic roster.
"""

import importlib.util
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from memory_service import db as db_mod
from memory_service import ledger

REPO = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "raise_approved_confidence",
        REPO / "scripts" / "raise_approved_confidence.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _approve(fid, code=200):
    return (f'INFO:     127.0.0.1:50101 - "POST /v1/facts/{fid}/approve '
            f'HTTP/1.1" {code} OK\n')


def _edit(fid, code=200, tail=""):
    return (f'INFO:     127.0.0.1:50102 - "PATCH /v1/facts/{fid}{tail} '
            f'HTTP/1.1" {code} OK\n')


def _noise():
    return ('INFO:     127.0.0.1:50103 - "GET /v1/facts?status=valid&limit=200 '
            'HTTP/1.1" 200 OK\n')


def _mined_low(con, settings, text):
    """A mined fact the miner held and the old approve released: low, live,
    no reason left."""
    fid = ledger.add_fact(
        con, text, settings, source="chat", origin_agent="multi-model-chat",
        source_app="multi-model-chat", confidence="low",
        quarantine_reason="grounding: Initech not in source chat"
                          + ledger.MINER_HOLD_SUFFIX)["id"]
    con.execute("UPDATE facts SET quarantined_at=NULL, quarantine_reason=NULL "
                "WHERE id=?", (fid,))
    con.commit()
    return fid


@pytest.fixture
def ledger_db(settings):
    """One fact per case, labelled, and a log over two files."""
    db_mod.init(settings)
    con = db_mod.connect(settings.db_path)
    f = {}
    f["raise mined"] = _mined_low(con, settings, "Alex restores old chisels.")
    f["raise older log"] = _mined_low(con, settings, "Alex keeps bees in Fairhaven.")
    f["raise mcp"] = ledger.add_fact(
        con, "Alex prefers dark roast coffee.", settings,
        origin_agent="mcp:claude-code")["id"]
    con.execute("UPDATE facts SET quarantined_at=NULL, quarantine_reason=NULL "
                "WHERE id=?", (f["raise mcp"],))
    f["edited after"] = _mined_low(con, settings, "Alex sharpens planes weekly.")
    f["edited before"] = _mined_low(con, settings, "Alex built a workbench.")
    f["held again"] = _mined_low(con, settings, "Alex visits Sam on Sundays.")
    ledger.quarantine_many(con, [f["held again"]], "person-forgotten: test")
    f["superseded"] = _mined_low(con, settings, "Alex drives a hatchback.")
    newer = ledger.add_fact(con, "Alex drives a ute now.", settings)["id"]
    ledger.mark_superseded(con, f["superseded"], newer)
    f["judge"] = _mined_low(con, settings, "Alex met Maya at AcmeCo.")
    con.execute("UPDATE facts SET quarantine_reason=? WHERE id=?",
                ("cleared by judge claude-haiku-4-5 2026-09-01: x", f["judge"]))
    f["already high"] = ledger.add_fact(
        con, "Alex works at Initech as a data engineer.", settings,
        source="chat", origin_agent="multi-model-chat",
        source_app="multi-model-chat")["id"]
    f["saved low"] = ledger.add_fact(
        con, "Alex might move to Globex next year.", settings,
        origin_agent="claude", source_app="multi-model-chat",
        confidence="low", web_sources=["example.com"])["id"]
    con.execute("UPDATE facts SET quarantined_at=NULL, quarantine_reason=NULL "
                "WHERE id=?", (f["saved low"],))
    f["erased"] = _mined_low(con, settings, "Alex once owned a kayak.")
    con.execute("DELETE FROM facts WHERE id=?", (f["erased"],))
    f["never approved"] = _mined_low(con, settings, "Alex likes Sicilian food.")
    f["approve failed"] = _mined_low(con, settings, "Alex plays chess.")
    # approved, then rebound to its chat: a scope change isn't an edit
    f["scope only"] = _mined_low(con, settings, "Alex reads about lathes.")
    con.commit()
    con.close()

    data = settings.data_dir
    (data / "service.log.1").write_text(
        "Starting Membro on http://127.0.0.1:8901 (admin UI at /) ...\n"
        + _approve(f["raise older log"]) + _noise())
    (data / "service.log").write_text(
        "2026-09-29 08:49:07,001 WARNING memory_service.summary: x\n"
        + _edit(f["edited before"])
        + "".join(_approve(f[k]) for k in (
            "raise mined", "raise mcp", "edited after", "edited before",
            "held again", "superseded", "judge", "already high", "saved low",
            "erased", "scope only"))
        + _approve(f["raise mined"])           # approved twice: one fact
        + _approve(f["approve failed"], 404)
        + _edit(f["edited after"])
        + _edit(f["raise mined"], 422)         # a refused edit isn't one
        + ('INFO:     127.0.0.1:50104 - "POST /v1/facts/'
           f'{f["scope only"]}/scope HTTP/1.1" 200 OK\n')
        + _noise())
    return f


RAISED = ("raise mined", "raise older log", "raise mcp", "scope only")


def _facts(con):
    return {r["id"]: dict(r) for r in con.execute("SELECT * FROM facts")}


def test_plan_raises_by_the_rule_and_counts_each_skip(settings, ledger_db):
    f = ledger_db
    mod = _script()
    log = mod.read_log([settings.data_dir / n for n in mod.LOG_NAMES])
    assert sorted(log["approvals"]) == sorted(
        f[k] for k in f if k not in ("never approved", "approve failed"))
    assert log["approval_lines"] == 13
    con = db_mod.connect(settings.db_path)
    try:
        raises, skipped = mod.plan(con, log)
    finally:
        con.close()
    assert [r["fact_id"] for r in raises] == sorted(f[k] for k in RAISED)
    assert dict(skipped) == {
        mod.MISSING: 1, mod.SUPERSEDED: 1, mod.HELD: 1, mod.JUDGE: 1,
        mod.EDITED_AFTER: 1, mod.EDITED_BEFORE: 1, mod.NOT_LOW: 1,
        mod.KEPT: 1}
    assert log["first_dated"] == "2026-09-29"
    assert [x["name"] for x in log["files"]] == ["service.log.1", "service.log"]


def test_dry_run_changes_nothing_and_prints_ids_only(settings, ledger_db):
    f = ledger_db
    mod = _script()
    con = db_mod.connect(settings.db_path)
    before = _facts(con)
    con.close()
    lines, probes = [], []
    assert mod.main([], settings=settings, out=lines.append,
                    probe=lambda s: probes.append(s) or []) == 0
    text = "\n".join(lines)
    for k in RAISED:
        assert f"fact {f[k]}  " in text
    assert ("12 fact(s) have a logged approval (13 approve line(s)): "
            "4 to raise to high, 8 left alone") in text
    assert "service.log.1: 3 lines" in text and "first dated line is 2026-09-29" in text
    for word in ("chisels", "bees", "coffee", "Fairhaven", "Maya", "Sam",
                 "Initech"):
        assert word not in text, word
    assert not probes
    con = db_mod.connect(settings.db_path)
    try:
        assert _facts(con) == before
    finally:
        con.close()
    assert not (settings.data_dir / "repairs.jsonl").exists()
    assert not list(settings.data_dir.glob("memory-before-confidence-*.db"))


def test_apply_raises_copies_journals_and_a_second_run_finds_nothing(
        settings, ledger_db):
    f = ledger_db
    mod = _script()
    con = db_mod.connect(settings.db_path)
    before = _facts(con)
    con.close()
    assert mod.main(["--apply"], settings=settings, out=lambda s: None,
                    probe=lambda s: None) == 0

    con = db_mod.connect(settings.db_path)
    try:
        after = _facts(con)
    finally:
        con.close()
    raised = {f[k] for k in RAISED}
    for fid, row in before.items():
        want = dict(row)
        if fid in raised:
            want["confidence"] = "high"
        assert after[fid] == want, fid       # nothing else ever changes

    data = settings.data_dir
    copies = list(data.glob("memory-before-confidence-*.db"))
    assert len(copies) == 1
    assert stat.S_IMODE(os.stat(copies[0]).st_mode) == 0o600
    held = sqlite3.connect(copies[0])
    try:
        assert {r[0] for r in held.execute(
            "SELECT id FROM facts WHERE confidence='low'")} >= raised
    finally:
        held.close()

    journal_text = (data / "repairs.jsonl").read_text()
    journal = [json.loads(line) for line in journal_text.splitlines()]
    assert {j["fact_id"] for j in journal} == raised
    for j in journal:
        assert j["repair"] == "raise-approved-confidence-195"
        assert j["action"] == "raised" and j["backup"] == copies[0].name
        assert (j["old_confidence"], j["new_confidence"]) == ("low", "high")
    for word in ("chisels", "bees", "coffee", "Fairhaven"):
        assert word not in journal_text, word
    assert stat.S_IMODE(os.stat(data / "repairs.jsonl").st_mode) == 0o600

    lines = []
    mod.main([], settings=settings, out=lines.append, probe=lambda s: None)
    assert any("nothing to raise" in line for line in lines)
    assert any(f"left alone, {mod.NOT_LOW}: 5" in line for line in lines)


def test_apply_goes_ahead_when_membro_is_idle(settings, ledger_db):
    mod = _script()
    assert mod.main(["--apply"], settings=settings, out=lambda s: None,
                    probe=lambda s: []) == 0
    assert (settings.data_dir / "repairs.jsonl").exists()


def _nothing_changed(settings, before):
    con = db_mod.connect(settings.db_path)
    try:
        assert _facts(con) == before
    finally:
        con.close()
    assert not list(settings.data_dir.glob("memory-before-confidence-*.db"))
    assert not (settings.data_dir / "repairs.jsonl").exists()


def test_apply_refuses_while_membro_is_busy(settings, ledger_db):
    mod = _script()
    con = db_mod.connect(settings.db_path)
    before = _facts(con)
    con.close()
    with pytest.raises(SystemExit, match="busy"):
        mod.main(["--apply"], settings=settings, out=lambda s: None,
                 probe=lambda s: ["summary"])
    _nothing_changed(settings, before)


def test_apply_refuses_when_membro_cannot_say(settings, ledger_db):
    mod = _script()
    con = db_mod.connect(settings.db_path)
    before = _facts(con)
    con.close()

    def broken(s):
        raise TimeoutError("no answer")

    with pytest.raises(SystemExit, match="nothing changed"):
        mod.main(["--apply"], settings=settings, out=lambda s: None,
                 probe=broken)
    _nothing_changed(settings, before)


def test_a_fact_that_changes_mid_apply_rolls_everything_back(
        settings, ledger_db, monkeypatch):
    f = ledger_db
    mod = _script()
    con = db_mod.connect(settings.db_path)
    before = _facts(con)
    con.close()
    real = mod.snapshot

    def snapshot_then_edit(s):
        path = real(s)
        c = db_mod.connect(s.db_path)
        c.execute("UPDATE facts SET confidence='medium' WHERE id=?",
                  (f["raise mcp"],))
        c.commit()
        c.close()
        return path

    monkeypatch.setattr(mod, "snapshot", snapshot_then_edit)
    with pytest.raises(SystemExit, match="nothing changed"):
        mod.main(["--apply"], settings=settings, out=lambda s: None,
                 probe=lambda s: None)
    con = db_mod.connect(settings.db_path)
    try:
        after = _facts(con)
    finally:
        con.close()
    for fid, row in before.items():
        if fid != f["raise mcp"]:
            assert after[fid] == row, fid
    assert not (settings.data_dir / "repairs.jsonl").exists()


def test_the_log_can_be_named_and_must_exist(settings, ledger_db, tmp_path):
    mod = _script()
    elsewhere = tmp_path / "other.log"
    elsewhere.write_text(_approve(ledger_db["raise mcp"]))
    lines = []
    mod.main(["--log", str(elsewhere)], settings=settings, out=lines.append)
    assert any("1 fact(s) have a logged approval" in line for line in lines)
    with pytest.raises(SystemExit, match="no service log"):
        mod.main(["--log", str(tmp_path / "missing.log")], settings=settings,
                 out=lambda s: None)


def test_a_missing_database_is_never_created(tmp_path, settings):
    mod = _script()
    elsewhere = settings.model_copy(update={"data_dir": tmp_path / "nowhere"})
    with pytest.raises(SystemExit, match="no database"):
        mod.main([], settings=elsewhere, out=lambda s: None)
    assert not (tmp_path / "nowhere" / "memory.db").exists()
