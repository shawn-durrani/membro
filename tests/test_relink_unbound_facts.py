"""scripts/relink_unbound_facts.py, the one-off repair for #146, and the
ledger's late binding it writes through.

From 13 August until #145 the miner stored most facts without the message
they came from. The script binds such a fact to one of the owner's own
turns, only in a chat with no guest speech, only when that turn clearly
shares the most distinctive words, and moves its date only when that turn
writes the fact's calendar date. Everything here runs on a throwaway
ledger with the synthetic roster: Alex owns it, Sam is a guest, Maya is
family.
"""

import datetime
import http.server
import importlib.util
import json
import os
import re
import socket
import sqlite3
import stat
import threading
from collections import Counter
from pathlib import Path

import pytest

from memory_service import db as db_mod
from memory_service import episodic, ledger, persons

REPO = Path(__file__).resolve().parents[1]
APP = "multi-model-chat"
T0 = datetime.datetime(2026, 9, 10, 9, 0).timestamp()
SAVED = T0 + 3600          # every turn below is said before this, unless noted


def _script():
    spec = importlib.util.spec_from_file_location(
        "relink_unbound_facts", REPO / "scripts" / "relink_unbound_facts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _chat(con, ref, turns):
    """Ingest one chat, a turn a minute from T0. A turn is (speaker, text)
    or (speaker, text, extra message fields). Returns the chat's id and
    each turn's message id, in order."""
    msgs = []
    for i, turn in enumerate(turns):
        speaker, text, extra = (*turn, {})[:3]
        msgs.append({"external_id": f"{ref}-{i}", "speaker": speaker,
                     "content": text, "created_at": T0 + 60 * i, **extra})
    cid = episodic.ingest(con, APP, ref, msgs)["conversation_id"]
    ids = [con.execute("SELECT id FROM messages WHERE conversation_id=? "
                       "AND external_id=?", (cid, m["external_id"])).fetchone()[0]
           for m in msgs]
    return cid, ids


def _fact(con, settings, cid, text, saved=SAVED, **kw):
    kw.setdefault("event_date", db_mod.day_start(saved))
    res = ledger.add_fact(con, text, settings, source="chat",
                          origin_agent=APP, source_app=APP,
                          conversation_id=cid, **kw)
    con.execute("UPDATE facts SET created_at=? WHERE id=?", (saved, res["id"]))
    con.commit()
    return res["id"]


def _day(month, day, year=2026):
    return datetime.datetime(year, month, day).timestamp()


@pytest.fixture
def ledger_db(settings, con):
    """One fact per rule and per skip reason. Returns {label: fact id} and
    {label: message id} for the turns the checks name."""
    f, m = {}, {}

    # A chat with no guests: each owner turn is Alex's.
    cid, ids = _chat(con, "home", [
        ("user", "We're repainting the garage door sage green before "
                 "Maya's party."),
        ("claude", "Sage green suits a garage door. Satin finish?"),
        ("user", "Satin, yes. I also booked the plumber for the leaking "
                 "laundry basin."),
        ("claude", "Noted the plumber."),
        ("user", "The plumber comes on 3 October."),
    ])
    m["garage"], m["basin"] = ids[0], ids[2]
    f["bind"] = _fact(con, settings, cid,
                      "Alex is repainting the garage door sage green.")
    # Its words sit in the model's turn, and only one in Alex's.
    f["model words"] = _fact(con, settings, cid,
                             "Alex chose a satin finish for the garage.")
    # The date is written in another turn, not the one it binds to.
    f["date elsewhere"] = _fact(
        con, settings, cid,
        "Alex booked a plumber for the leaking laundry basin on 3 October.")
    f["held"] = _fact(con, settings, cid,
                      "Alex is repainting the garage door sage green soon.",
                      quarantine_reason="grounding: test hold")
    f["superseded"] = _fact(con, settings, cid,
                            "Alex was repainting the garage door sage green.")
    ledger.mark_superseded(con, f["superseded"], f["bind"])
    f["has source"] = _fact(con, settings, cid,
                            "Alex booked the plumber for the laundry basin.",
                            source_message_id=ids[2])
    f["legacy"] = _fact(con, settings, cid,
                        "Alex is repainting the garage door sage green again.",
                        saved=datetime.datetime(2026, 7, 1).timestamp())

    # Dates: one moves, one names two days, one is already right.
    cid, ids = _chat(con, "dates", [
        ("user", "Sam's citizenship ceremony is on 14 October at the "
                 "Fairhaven town hall."),
        ("gpt", "Congratulations to Sam!"),
        ("user", "Car registration renewal window runs from 14 October to "
                 "20 October."),
        ("user", "Maya's violin recital is on 2 November at Globex hall."),
    ])
    m["ceremony"], m["registration"], m["recital"] = ids[0], ids[2], ids[3]
    f["date moves"] = _fact(
        con, settings, cid,
        "Alex's partner Sam has a citizenship ceremony at Fairhaven town "
        "hall on 14 October.")
    f["two dates"] = _fact(
        con, settings, cid,
        "Alex's car registration renewal window runs between 14 October and "
        "20 October.")
    f["date right"] = _fact(
        con, settings, cid,
        "Alex's daughter Maya has a violin recital at Globex hall on "
        "2 November.", event_date=_day(11, 2))

    # A guest spoke before the fact was saved, or after it: both skip.
    cid, _ = _chat(con, "kayak", [
        ("user", "Our kayak trip to Fairhaven Lake starts Friday."),
        ("guest:Sam", "I packed the kayak paddles."),
    ])
    f["guest"] = _fact(con, settings, cid,
                       "Alex is going on a kayak trip to Fairhaven Lake.")
    cid, _ = _chat(con, "orchard", [
        ("user", "Planting the quince orchard along the northern fence."),
    ])
    episodic.ingest(con, APP, "orchard", [
        {"external_id": "late", "speaker": "guest:unknown",
         "content": "Nice fence.", "created_at": SAVED + 600}])
    f["guest later"] = _fact(con, settings, cid,
                             "Alex is planting a quince orchard by the fence.")

    # Alex's only matching turn came after the fact was saved.
    cid, _ = _chat(con, "chess", [
        ("claude", "The Sicilian defence suits aggressive chess players."),
    ])
    episodic.ingest(con, APP, "chess", [
        {"external_id": "late", "speaker": "user",
         "content": "I'm learning the Sicilian defence for chess club.",
         "created_at": SAVED + 600}])
    f["no turn"] = _fact(con, settings, cid,
                         "Alex is learning the Sicilian defence for chess club.")

    # Two of Alex's turns share the fact's words about equally.
    cid, _ = _chat(con, "vise", [
        ("user", "The workbench vise needs replacement jaws."),
        ("user", "Ordered replacement jaws for the workbench vise."),
    ])
    f["no lead"] = _fact(con, settings, cid,
                         "Alex needs replacement jaws for the workbench vise.")

    # A word in three of Alex's turns in the chat doesn't count.
    cid, _ = _chat(con, "pottery", [
        ("user", "Glazing pottery tonight."),
        ("user", "Pottery class moved rooms."),
        ("user", "The pottery wheel arrived."),
    ])
    f["common in chat"] = _fact(con, settings, cid, "Alex is glazing pottery.")

    # A word in more than 20 of Alex's turns overall doesn't count either.
    _chat(con, "cafe", [("user", f"Espresso number {i}.") for i in range(21)])
    cid, ids = _chat(con, "grinder", [
        ("user", "The espresso grinder burr is worn."),
    ])
    m["grinder"] = ids[0]
    f["common overall"] = _fact(con, settings, cid,
                                "Alex's espresso grinder burr is worn.")

    # The turn carries a web stamp, so binding it would hold the fact.
    cid, _ = _chat(con, "ferry", [
        ("user", "Read that the Fairhaven ferry timetable changes in spring.",
         {"web_sources": ["example.org"]}),
    ])
    f["web stamp"] = _fact(con, settings, cid,
                           "Alex read the Fairhaven ferry timetable changes.")
    return f, m


def _says(text, word):
    return re.search(rf"\b{word}\b", text, re.I) is not None


def _facts(con):
    return {r["id"]: dict(r) for r in con.execute("SELECT * FROM facts")}


EXPECTED_BINDS = {"bind": ("garage", 5), "date moves": ("ceremony", 6),
                  "two dates": ("registration", 5),
                  "date right": ("recital", 6),
                  "date elsewhere": ("basin", 5),
                  "common overall": ("grinder", 3)}
EXPECTED_SKIPS = {
    "held": "held for review", "superseded": "superseded",
    "guest": "chat has guest speech",
    "guest later": "chat has guest speech",
    "no turn": "none of your turns before it was saved",
    "model words": "fewer than two distinctive words shared",
    "common in chat": "fewer than two distinctive words shared",
    "no lead": "no clear lead over another turn",
    "web stamp": "the turn would change more than the link",
}


def test_plan_binds_by_the_rule_and_counts_each_skip(settings, ledger_db):
    f, m = ledger_db
    mod = _script()
    con = db_mod.connect(settings.db_path)
    try:
        binds, skipped, dates = mod.plan(con, settings)
    finally:
        con.close()
    got = {b["fact_id"]: (b["message_id"], b["shared"]) for b in binds}
    assert got == {f[label]: (m[turn], shared)
                   for label, (turn, shared) in EXPECTED_BINDS.items()}
    assert skipped == Counter(EXPECTED_SKIPS.values())
    # Facts with a source already, or from before 13 August, aren't looked at.
    assert f["has source"] not in got and f["legacy"] not in got
    assert dates == Counter({"date changes": 1, "date already right": 1,
                             "several dates in the turn, date kept": 1,
                             "the fact's date isn't in the turn": 1,
                             "no date in the fact": 2})
    by_fact = {b["fact_id"]: b for b in binds}
    assert by_fact[f["date moves"]]["new_event_date"] == _day(10, 14)
    for label in ("bind", "two dates", "date right", "date elsewhere"):
        assert by_fact[f[label]]["new_event_date"] is None


def test_distinctive_words(settings, con):
    mod = _script()
    allow = {"alex", "acme"}
    in_chat = Counter({"glazing": 1, "pottery": 3, "kiln": 2})
    overall = Counter({"glazing": 4, "pottery": 9, "kiln": 21})
    words = mod.distinctive(
        "Alex really wants something glazing pottery kiln at Acme", allow,
        in_chat, overall, limit=20)
    # Not a common word, not the owner's name or the allowlist, in at most
    # two of the owner's turns in the chat and at most `limit` overall.
    assert words == {"glazing", "wants"}
    assert mod.overall_limit(100) == 20
    assert mod.overall_limit(18_000) == 90


def test_grounded_day_follows_the_miners_rule():
    mod = _script()
    said = T0
    assert mod.grounded_day("Alex likes tea.", "on 3 October", said) \
        == ("no date in the fact", None)
    assert mod.grounded_day("Due 3 October.", "Due soon.", said) \
        == ("the fact's date isn't in the turn", None)
    # A year the turn doesn't give comes from when it was said.
    assert mod.grounded_day("Due 3 October.", "It's due October 3rd.", said) \
        == ("date changes", _day(10, 3))
    # A year the turn does give is the one kept.
    assert mod.grounded_day("Due 3 October.", "Due 3 October 2027.", said) \
        == ("date changes", _day(10, 3, 2027))
    assert mod.grounded_day("Due 3 October 2027.", "Due 3 October 2028.",
                            said) == ("the fact's date isn't in the turn", None)
    assert mod.grounded_day("From 3 October to 9 October.",
                            "3 October until 9 October", said) \
        == ("several dates in the turn, date kept", None)


def test_dry_run_changes_nothing_and_prints_no_text(settings, ledger_db):
    f, m = ledger_db
    mod = _script()
    con = db_mod.connect(settings.db_path)
    before = _facts(con)
    con.close()
    lines, probes = [], []
    assert mod.main([], settings=settings, out=lines.append,
                    probe=lambda s: probes.append(s) or []) == 0
    text = "\n".join(lines)
    for label, (turn, shared) in EXPECTED_BINDS.items():
        assert (f"fact {f[label]}  chat " in text
                and f"message {m[turn]}  shared {shared}" in text)
    for word in ("repainting", "sage", "plumber", "citizenship", "kayak",
                 "Sicilian", "espresso", "Maya", "Sam", "October"):
        assert not _says(text, word), word
    assert "15 mined fact(s) since 2026-08-13 have no source message: 6 to bind, 9 left alone" in text
    assert not probes  # a dry run never asks the service anything
    con = db_mod.connect(settings.db_path)
    try:
        assert _facts(con) == before
    finally:
        con.close()
    data = settings.data_dir
    assert not (data / "repairs.jsonl").exists()
    assert not list(data.glob("memory-before-relink-*.db"))


def test_apply_binds_copies_journals_and_a_second_run_finds_nothing(
        settings, ledger_db):
    f, m = ledger_db
    mod = _script()
    con = db_mod.connect(settings.db_path)
    before = _facts(con)
    messages = con.execute("SELECT * FROM messages ORDER BY id").fetchall()
    con.close()

    lines = []
    assert mod.main(["--apply"], settings=settings, out=lines.append,
                    probe=lambda s: None) == 0

    con = db_mod.connect(settings.db_path)
    try:
        after = _facts(con)
        assert con.execute("SELECT * FROM messages ORDER BY id").fetchall() \
            == messages
    finally:
        con.close()
    bound = {f[label]: m[turn] for label, (turn, _) in EXPECTED_BINDS.items()}
    for fid, row in before.items():
        want = dict(row)
        if fid in bound:
            want["source_message_id"] = bound[fid]
            if fid == f["date moves"]:
                want["event_date"] = _day(10, 14)
        # Nothing but the source link and the date ever changes.
        assert after[fid] == want, fid

    data = settings.data_dir
    copies = list(data.glob("memory-before-relink-*.db"))
    assert len(copies) == 1
    assert stat.S_IMODE(os.stat(copies[0]).st_mode) == 0o600
    held = sqlite3.connect(copies[0])
    try:
        assert held.execute("SELECT count(*) FROM facts WHERE "
                            "source_message_id IS NULL").fetchone()[0] \
            == sum(1 for r in before.values() if r["source_message_id"] is None)
    finally:
        held.close()

    journal_text = (data / "repairs.jsonl").read_text()
    journal = [json.loads(line) for line in journal_text.splitlines()]
    assert {j["fact_id"]: j["message_id"] for j in journal} == bound
    for j in journal:
        assert j["action"] == "bound" and j["backup"] == copies[0].name
        assert j["repair"] == "relink-unbound-facts-146"
        assert j["date_changed"] == (j["fact_id"] == f["date moves"])
    for word in ("repainting", "sage", "citizenship", "Fairhaven", "Maya"):
        assert not _says(journal_text, word), word
    assert stat.S_IMODE(os.stat(data / "repairs.jsonl").st_mode) == 0o600

    # A second run finds nothing left to bind.
    lines = []
    mod.main([], settings=settings, out=lines.append, probe=lambda s: None)
    assert any("nothing to bind" in line for line in lines)
    assert any("0 to bind" in line for line in lines)


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
    assert not list(settings.data_dir.glob("memory-before-relink-*.db"))
    assert not (settings.data_dir / "repairs.jsonl").exists()


def test_apply_refuses_while_membro_is_busy(settings, ledger_db):
    mod = _script()
    con = db_mod.connect(settings.db_path)
    before = _facts(con)
    con.close()
    with pytest.raises(SystemExit, match="busy"):
        mod.main(["--apply"], settings=settings, out=lambda s: None,
                 probe=lambda s: ["distill"])
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


def test_a_missing_database_is_never_created(tmp_path, settings):
    mod = _script()
    elsewhere = settings.model_copy(update={"data_dir": tmp_path / "nowhere"})
    with pytest.raises(SystemExit, match="no database"):
        mod.main([], settings=elsewhere, out=lambda s: None)
    assert not (tmp_path / "nowhere" / "memory.db").exists()


def test_probe_reads_the_busy_route_and_a_closed_port(settings):
    class Busy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"busy": True, "reasons": ["judge"]}).encode()
            self.send_response(200 if self.path == "/v1/busy" else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Busy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    mod = _script()
    try:
        here = settings.model_copy(update={"port": server.server_address[1]})
        assert mod.running_work(here) == ["judge"]
    finally:
        server.shutdown()
        server.server_close()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert mod.running_work(settings.model_copy(update={"port": free})) is None


# ---- the ledger's late binding ----

def test_bind_source_changes_only_the_link_and_the_date(settings, con):
    cid, ids = _chat(con, "home", [
        ("user", "We're repainting the garage door sage green."),
        ("claude", "Sage green it is."),
    ])
    other, other_ids = _chat(con, "elsewhere", [("user", "Unrelated turn.")])
    fid = _fact(con, settings, cid, "Alex is repainting the garage door.")
    before = ledger.get_fact(con, fid)

    # Not the fact's own chat: nothing written.
    assert ledger.bind_source(con, fid, other_ids[0]) is False
    assert ledger.bind_source(con, fid, ids[0], event_date=_day(10, 1)) is True
    con.commit()
    after = ledger.get_fact(con, fid)
    assert after == {**before, "source_message_id": ids[0],
                     "event_date": _day(10, 1)}
    # Already bound: never rebound.
    assert ledger.bind_source(con, fid, ids[1]) is False


def test_bind_source_leaves_held_and_superseded_facts(settings, con):
    cid, ids = _chat(con, "home", [("user", "Repainting the garage door.")])
    held = _fact(con, settings, cid, "Alex is repainting the garage door.",
                 quarantine_reason="grounding: test hold")
    old = _fact(con, settings, cid, "Alex repaints the garage door.")
    new = _fact(con, settings, cid, "Alex repainted the garage door.")
    ledger.mark_superseded(con, old, new)
    assert ledger.bind_source(con, held, ids[0]) is False
    assert ledger.bind_source(con, old, ids[0]) is False


def test_bind_source_refuses_a_turn_that_brings_more_than_the_link(
        settings, con):
    persons.upsert(con, settings, slug="alex", display_name="Alex")
    con.commit()
    cid, ids = _chat(con, "mixed", [
        ("guest:Sam", "Maya starts at the pottery studio on Monday."),
        ("user", "Read the pottery studio timetable online.",
         {"web_sources": ["example.org"]}),
        ("user", "Pottery studio fees went up.",
         {"speaker_identity": {"person": "alex", "method": "introduced"}}),
        ("claude", "The pottery studio sounds lovely."),
    ])
    fid = _fact(con, settings, cid, "Maya starts at the pottery studio.")
    for message_id, reason in ((ids[0], "guest"), (ids[1], "web stamp"),
                               (ids[2], "link the fact to a person")):
        assert reason in ledger.bind_refusal(con, message_id)
        with pytest.raises(ValueError, match=reason):
            ledger.bind_source(con, fid, message_id)
    assert ledger.bind_refusal(con, 999_999) == "no such message"
    # A model's plain turn brings nothing else. Picking whose turn a fact
    # binds to is the caller's rule.
    assert ledger.bind_refusal(con, ids[3]) is None
    assert ledger.get_fact(con, fid)["source_message_id"] is None
