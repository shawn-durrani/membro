"""One-off repair for #146: give mined facts back the message they came from.

From 13 August the miner stored a fact without its source message whenever
it couldn't read the model's src= tag, and until #145 it couldn't read a
bracketed one, which was most of them. Each such fact still knows its chat.
It lost the link to the one message that said it, so erasing that message
doesn't send it back to review. It also lost any date the model gave it,
because a date is kept only when that one message writes it.

The model's replies aren't kept, so the tags can't be read back. This
script makes a careful guess instead, and only where a guess can't change
who a fact is about. It binds a fact:

- only in a chat with no guest speech at all, so every human turn is yours,
- only to one of your own turns said before the fact was saved, never a
  model's or a guest's,
- only when that turn shares at least two distinctive words with the fact,
  and at least two more than any other of your turns in that chat.

A distinctive word is one `walls.support_tokens` keeps (four letters or
more, not a common word, not your name, the grounding allowlist or a name
membro holds for a person) that's also rare. It's in no more than two of
your turns in that chat, and in no more than 0.5% of all your turns (or 20,
whichever is more). A word you use all the time can't tie a fact to one
turn.

The date moves only when the fact names one calendar date and the chosen
turn writes it, by the miner's own rule (mining._grounded_event_date).

Dry run by default: each fact it would bind prints as its id, its chat, the
message, how many distinctive words they share and whether the date would
change, never any text. The facts it leaves alone are counted by reason.
With --apply it refuses while membro is busy, saves a copy of memory.db in
data/ first, binds through ledger.bind_source in one transaction, and
appends one content-free line per fact to data/repairs.jsonl.

It never touches a fact that already has a source, a fact held for review
or a superseded one, and it changes nothing but a fact's source link and
its date.

Run:  .venv/bin/python scripts/relink_unbound_facts.py [--apply] [--data-dir PATH]
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_service import db, episodic, ledger, mining, walls  # noqa: E402
from memory_service.config import load_settings  # noqa: E402

REPAIR = "relink-unbound-facts-146"
JOURNAL_NAME = "repairs.jsonl"

# The miner began storing a fact with no source on 13 August 2026. Before
# that, such a fact carried a stand-in, and a fact with no source at all is
# a legacy import that predates provenance. Neither is this repair's.
SINCE = datetime.datetime(2026, 8, 13).timestamp()

CHAT_LIMIT = 2          # a distinctive word is in at most 2 of your turns in the chat
OVERALL_SHARE = 0.005   # and in at most 0.5% of all your turns
OVERALL_FLOOR = 20      # or 20 of them, whichever is more
MIN_SHARED = 2          # the chosen turn shares at least this many
MIN_LEAD = 2            # and this many more than any other of your turns

HELD = "held for review"
SUPERSEDED = "superseded"
GUEST = "chat has guest speech"
NO_TURN = "none of your turns before it was saved"
WEAK = "fewer than two distinctive words shared"
NO_LEAD = "no clear lead over another turn"
REFUSED = "the turn would change more than the link"
SKIPS = (HELD, SUPERSEDED, GUEST, NO_TURN, WEAK, NO_LEAD, REFUSED)

NO_DATE = "no date in the fact"
NOT_IN_TURN = "the fact's date isn't in the turn"
SEVERAL = "several dates in the turn, date kept"
SAME_DAY = "date already right"
MOVES = "date changes"
DATES = (MOVES, SAME_DAY, NO_DATE, NOT_IN_TURN, SEVERAL)


def overall_limit(owner_turns: int) -> int:
    return max(OVERALL_FLOOR, int(owner_turns * OVERALL_SHARE))


def distinctive(text: str, allow: set[str], in_chat: Counter,
                overall: Counter, limit: int) -> set[str]:
    """The words of `text` that can tie it to one turn: what
    walls.support_tokens keeps, minus any word in more than CHAT_LIMIT of
    your turns in the chat (`in_chat`) or more than `limit` of all your
    turns (`overall`)."""
    return {w for w in walls.support_tokens(text, allow)
            if in_chat[w] <= CHAT_LIMIT and overall[w] <= limit}


def grounded_day(fact: str, body: str, said_at: float) -> tuple[str, float | None]:
    """The calendar day the fact's own words name and the turn writes, by
    the miner's rule, with the outcome label. A date written without a year
    takes the year the turn gives it, else the year the turn was said."""
    named = walls.explicit_dates(fact)
    if not named:
        return NO_DATE, None
    in_turn = walls.explicit_dates(body)
    said_year = datetime.datetime.fromtimestamp(said_at).year
    days = set()
    for month, day, year in named:
        years = {year} if year else (
            {y for m, d, y in in_turn if (m, d) == (month, day) and y}
            | {said_year})
        for y in years:
            found = mining._grounded_event_date(
                f"{y:04d}-{month:02d}-{day:02d}", body, said_at)
            if found is not None:
                days.add(found)
    if not days:
        return NOT_IN_TURN, None
    if len(days) > 1:
        return SEVERAL, None
    return MOVES, days.pop()


def _chat_view(con, conversation_id: int) -> list[dict]:
    """A chat's turns as the miner reads them: each message with its
    attachment text, through mining._msg_body."""
    atts = episodic.attachments_for_conversation(con, conversation_id)
    turns = episodic.messages_after(con, conversation_id, 0)
    for t in turns:
        t["attachments"] = atts.get(t["external_id"], [])
        t["body"] = mining._msg_body(t)
    return turns


def plan(con, settings) -> tuple[list[dict], Counter, Counter]:
    """Every fact the rule binds, oldest first, with what the dry run shows,
    plus the skips and the bound facts' date outcomes counted. Text is read
    here and never returned."""
    allow = mining.grounding_allow(con, settings)
    facts = [dict(r) for r in con.execute(
        "SELECT id, conversation_id, content, created_at, event_date, "
        "invalidated_at, quarantined_at FROM facts "
        "WHERE source='chat' AND source_message_id IS NULL "
        "AND conversation_id IS NOT NULL AND created_at >= ? ORDER BY id",
        (SINCE,))]
    wanted = {f["conversation_id"] for f in facts}

    # Every one of your turns, in every chat, for the overall count.
    chats: dict[int, list[dict]] = {}
    overall: Counter = Counter()
    owner_turns = 0
    for (cid,) in con.execute(
            "SELECT DISTINCT conversation_id FROM messages").fetchall():
        turns = _chat_view(con, cid)
        for t in turns:
            t["owner"] = walls.speaker_class(t["speaker"]) == "owner"
            if t["owner"]:
                t["words"] = walls.support_tokens(t["body"], allow)
                overall.update(t["words"])
                owner_turns += 1
        if cid in wanted:
            chats[cid] = turns
    limit = overall_limit(owner_turns)

    binds, skipped, dates = [], Counter(), Counter()
    for f in facts:
        turns = chats.get(f["conversation_id"], [])
        if f["quarantined_at"] is not None:
            skipped[HELD] += 1
            continue
        if f["invalidated_at"] is not None:
            skipped[SUPERSEDED] += 1
            continue
        if any(walls.speaker_trust_flag(t["speaker"]) for t in turns):
            skipped[GUEST] += 1
            continue
        mine = [t for t in turns
                if t["owner"] and t["created_at"] <= f["created_at"]]
        if not mine:
            skipped[NO_TURN] += 1
            continue
        in_chat = Counter(w for t in mine for w in t["words"])
        words = distinctive(f["content"], allow, in_chat, overall, limit)
        scores = sorted(((len(words & t["words"]), t["id"]) for t in mine),
                        key=lambda s: (-s[0], s[1]))
        best, message_id = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else 0
        if best < MIN_SHARED:
            skipped[WEAK] += 1
            continue
        if best - runner_up < MIN_LEAD:
            skipped[NO_LEAD] += 1
            continue
        if ledger.bind_refusal(con, message_id):
            skipped[REFUSED] += 1
            continue
        turn = next(t for t in mine if t["id"] == message_id)
        outcome, day = grounded_day(f["content"], turn["body"],
                                    turn["created_at"])
        if day is not None and day == f["event_date"]:
            outcome, day = SAME_DAY, None
        dates[outcome] += 1
        binds.append({
            "fact_id": f["id"], "conversation_id": f["conversation_id"],
            "message_id": message_id, "shared": best,
            "old_event_date": f["event_date"], "new_event_date": day})
    return binds, skipped, dates


def describe(b: dict) -> str:
    moved = "changes" if b["new_event_date"] is not None else "kept"
    return (f"fact {b['fact_id']}  chat {b['conversation_id']}  "
            f"message {b['message_id']}  shared {b['shared']}  date {moved}")


def running_work(settings):
    """membro's busy reasons, [] when idle, or None when nothing answers on
    its port. Any other answer raises: an --apply that can't tell whether
    membro is mid-work doesn't go ahead."""
    url = f"http://{settings.host}:{settings.port}/v1/busy"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return list(json.load(resp).get("reasons") or [])
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            return None
        raise


def snapshot(settings) -> Path:
    """A consistent copy of memory.db in data/, taken with SQLite's online
    backup like the service's own snapshots, owner-only. It sits outside
    data/backups, so the snapshot rotation never removes it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = settings.data_dir / f"memory-before-relink-{stamp}.db"
    n = 1
    while dest.exists():
        n += 1
        dest = settings.data_dir / f"memory-before-relink-{stamp}-{n}.db"
    src = db.connect(settings.db_path)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    os.chmod(dest, 0o600)
    return dest


def apply(con, settings, binds: list[dict], backup: Path) -> None:
    """Bind every fact in one transaction, each re-checked first, and
    journal each change without any text."""
    ids = [b["fact_id"] for b in binds]
    held = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        kept = held.execute(
            f"SELECT count(*) FROM facts WHERE source_message_id IS NULL "
            f"AND id IN ({','.join('?' * len(ids))})", ids).fetchone()[0]
    finally:
        held.close()
    if kept != len(ids):
        raise SystemExit(f"the copy {backup} doesn't hold every fact "
                         "unbound; nothing changed")
    again, _, _ = plan(con, settings)
    if again != binds:
        raise SystemExit("the facts changed since the dry run; nothing "
                         "changed, run it again")
    try:
        for b in binds:
            if not ledger.bind_source(con, b["fact_id"], b["message_id"],
                                      event_date=b["new_event_date"]):
                raise SystemExit(f"fact {b['fact_id']} changed while "
                                 "binding; nothing changed, run it again")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    at = time.time()
    with open(settings.data_dir / JOURNAL_NAME, "a") as journal:
        for b in binds:
            line = {"at": at, "repair": REPAIR, "action": "bound",
                    "fact_id": b["fact_id"],
                    "conversation_id": b["conversation_id"],
                    "message_id": b["message_id"], "shared": b["shared"],
                    "date_changed": b["new_event_date"] is not None,
                    "backup": backup.name}
            if b["new_event_date"] is not None:
                line["old_event_date"] = b["old_event_date"]
                line["new_event_date"] = b["new_event_date"]
            journal.write(json.dumps(line) + "\n")


def main(argv=None, settings=None, probe=running_work, out=print) -> int:
    ap = argparse.ArgumentParser(
        description="Give mined facts back the message they came from (#146).")
    ap.add_argument("--apply", action="store_true",
                    help="bind the facts; the default is a dry run")
    ap.add_argument("--data-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    settings = settings or load_settings()
    if args.data_dir:
        settings = settings.model_copy(
            update={"data_dir": args.data_dir.resolve()})
    if not settings.db_path.exists():
        # db.connect() would create an empty database here.
        raise SystemExit(f"no database at {settings.db_path}")

    mode = "APPLY" if args.apply else "dry-run"
    con = db.connect(settings.db_path)
    try:
        binds, skipped, dates = plan(con, settings)
        for b in binds:
            out(f"[{mode}] {describe(b)}")
        total = len(binds) + sum(skipped.values())
        since = datetime.date.fromtimestamp(SINCE).isoformat()
        out(f"[{mode}] {total} mined fact(s) since {since} have no source "
            f"message: {len(binds)} to bind, {sum(skipped.values())} left "
            "alone")
        for reason in SKIPS:
            if skipped[reason]:
                out(f"[{mode}]   left alone, {reason}: {skipped[reason]}")
        for outcome in DATES:
            if dates[outcome]:
                out(f"[{mode}]   bound, {outcome}: {dates[outcome]}")
        if not binds:
            out(f"[{mode}] nothing to bind; nothing to do")
            return 0
        if not args.apply:
            out("[dry-run] nothing changed; run again with --apply")
            return 0

        try:
            work = probe(settings)
        except Exception as e:
            raise SystemExit(f"couldn't ask membro on :{settings.port} "
                             f"whether it's busy ({type(e).__name__}); "
                             "nothing changed")
        if work:
            raise SystemExit(f"membro is busy ({', '.join(work)}); nothing "
                             "changed, run it again when it's idle")
        # The copy and the journal sit beside the memory: owner-only.
        db.secure_data_dir(settings)
        backup = snapshot(settings)
        out(f"[APPLY] copy saved: {backup}")
        apply(con, settings, binds, backup)
        out(f"[APPLY] bound {len(binds)} fact(s); journal: "
            f"{settings.data_dir / JOURNAL_NAME}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
