"""MCP adapter — the four model-facing tools ONLY.

Admin operations (approve / dismiss / delete / ingest / consolidate) are
deliberately absent: that asymmetry is the write gate. Every save through this
adapter carries an mcp:* origin and is therefore quarantined at creation —
nothing external becomes canon without human review.

Register with (the variables must be passed with -e so they reach the SERVER
when it runs; a shell prefix before `claude mcp add` would only set them for
the registration command itself):
    claude mcp add -s user membro -e PYTHONPATH=<repo> -- \
        <repo>/.venv/bin/python -m memory_service.mcp_server
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from memory_service import access, db, episodic, ledger as ledger_mod  # noqa: E402
from memory_service import recall as recall_mod, summary as summary_mod  # noqa: E402
from memory_service import walls  # noqa: E402
from memory_service.config import load_settings  # noqa: E402

mcp = FastMCP("multi-model-memory")
SETTINGS = load_settings()
CLIENT = os.environ.get("MEMORY_MCP_CLIENT", "claude-code")
ORIGIN = f"mcp:{CLIENT}"  # access-log origin: these lookups happen in THIS
                          # process, invisible to the service without the log


def _con():
    return db.connect(SETTINGS.db_path)


def _fmt(facts: list[dict]) -> str:
    lines = []
    for f in facts:
        day = datetime.date.fromtimestamp(f.get("event_date") or f["created_at"]).isoformat()
        if f.get("confidence") == "low":
            day += "?"
        flag = ""
        if f.get("invalidated_at"):
            until = datetime.date.fromtimestamp(f["invalidated_at"]).isoformat()
            flag = f" [SUPERSEDED {until} — historical, no longer current]"
        lines.append(f"[{day} ·{f.get('origin_agent', '')}] {f['content']}{flag}")
    return "\n".join(lines)


@mcp.tool()
def recall_memory(query: str = "") -> str:
    """Search the user's complete memory ledger — the durable, never-summarized
    record of facts about them (hybrid semantic + keyword search). Empty query
    returns the most recent entries."""
    con = _con()
    try:
        facts = recall_mod.recall(con, SETTINGS, query, limit=20)
        access.record(con, "recall", query, origin=ORIGIN, facts=facts)
    finally:
        con.close()
    return _fmt(facts) if facts else "No matching memory entries."


@mcp.tool()
def search_history(query: str) -> str:
    """Verbatim full-text search across every ingested message in the user's
    conversation history."""
    con = _con()
    try:
        hits = episodic.search(con, query)
        access.record(con, "search", query, origin=ORIGIN, count=len(hits))
    finally:
        con.close()
    if not hits:
        return "No matching messages."
    return "\n".join(
        f"[{datetime.date.fromtimestamp(h['created_at']).isoformat()}] "
        f"\"{h['title']}\" — {h['speaker']}: {h['content']}" for h in hits)


@mcp.tool()
def save_memory(content: str, event_date: str = "") -> str:
    """Propose a durable fact about the user for their permanent memory ledger
    (one clear sentence). External saves are held for the user's review before
    becoming part of their memory. Optionally pass event_date as YYYY-MM-DD when
    the fact is about a specific past/future date rather than today."""
    # Drop, don't quarantine, obvious system/AI-tooling or builder-process noise
    # (#66) — mcp:* writes are quarantined by provenance regardless of content,
    # so without this check even blatant process chatter ("merged PR #12, CI is
    # green") would land straight in the human review queue. Quarantine means
    # an uncertain, potentially durable fact about the user, never an audit
    # trail of a bot's bad instincts, so this is a silent drop, same as mining.
    if walls.is_system_meta(content):
        walls.record_drop("system-meta")
        return ("Not saved: reads as this memory system's own machinery or the "
                "AI tooling itself, not a fact about you.")
    if walls.is_builder_process(content):
        walls.record_drop("builder-process")
        return ("Not saved: reads as engineering-process chatter (PR/issue/CI "
                "mechanics, implementation minutiae, UI styling tweaks), not a "
                "durable fact about you.")
    ts = None
    if event_date.strip():
        try:
            ts = datetime.datetime.fromisoformat(event_date.strip()).timestamp()
        except ValueError:
            ts = None
    con = _con()
    try:
        res = ledger_mod.add_fact(con, content, SETTINGS, source="model",
                                  origin_agent=f"mcp:{CLIENT}", event_date=ts)
    except ValueError as e:
        return f"Error: {e}"
    finally:
        con.close()
    return (f"Saved, held for the user's review (fact {res['id']}): {content}"
            if res["quarantined"] else f"Saved to memory ledger: {content}")


def _format_summary(s: dict) -> str:
    """Stamp the profile prose with when it was generated. A stale summary reads
    as authoritative, so the consumer is told the age and reminded to verify
    time-sensitive / active-thread status with recall (see issue #27)."""
    if not s.get("summary"):
        return "No memory summary yet."
    ts = s.get("generated_at")
    try:
        stamp = datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        stamp = "unknown"
    header = (
        f"Profile summary generated_at: {stamp}\n"
        "For time-sensitive or active-thread status, verify with recall.\n\n"
    )
    return header + s["summary"]


@mcp.tool()
def memory_summary() -> str:
    """The current structured profile summary of the user, rebuilt from their
    memory ledger (newest facts win). The output is stamped with when the
    summary was generated — a stale summary reads as authoritative, so
    time-sensitive or active-thread status should be verified with recall."""
    con = _con()
    try:
        s = summary_mod.get(con)
        access.record(con, "summary", origin=ORIGIN)
    finally:
        con.close()
    return _format_summary(s)


if __name__ == "__main__":
    mcp.run()
