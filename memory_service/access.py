"""The access log — the read-side twin of the ledger.

Writes have always been ground truth here (append-only facts, immutable
messages); reads used to vanish. This module records every lookup against
memory — deep recalls, history searches, summary fetches — from the HTTP API
and every MCP client process alike, into one append-only table: when, what was
asked, which facts came back. Nothing in the service updates or deletes a row.

Consumers read it (the /math live view today; query-history replay and
reinforce-on-reuse are designed against it) — the read path never depends on
them: a failed log write is swallowed, a recall must never fail because its
footprint couldn't be recorded.
"""

import json
import logging
import sqlite3

from . import db

log = logging.getLogger("memory_service.access")

QUERY_CAP = 500  # sanity bound; recall queries are sentence-sized

# Also part of db.SCHEMA; kept here so an MCP adapter process (which never runs
# db.init) can lazily create the table when it reaches a not-yet-migrated DB.
TABLE = """
CREATE TABLE IF NOT EXISTS access_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,                     -- recall | search | summary
  origin TEXT NOT NULL DEFAULT 'http',    -- http | mcp:<client-name>
  query TEXT NOT NULL DEFAULT '',
  result_ids TEXT NOT NULL DEFAULT '[]',  -- facts returned: [[fact_id, score|null], ...]
  result_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_access_log_ts ON access_log(ts);
"""

_INSERT = ("INSERT INTO access_log(ts, kind, origin, query, result_ids, "
           "result_count) VALUES(?,?,?,?,?,?)")


def record(con, kind: str, query: str = "", origin: str = "http",
           facts: list[dict] | None = None, count: int | None = None) -> None:
    """Append one lookup event. `facts` are the recall results (ids + scores
    recorded — the raw material of reinforce-on-reuse); `count` covers kinds
    whose results aren't facts (history search returns messages)."""
    ids = [[f["id"], f.get("score")] for f in (facts or [])]
    row = (db.now(), kind, origin, (query or "")[:QUERY_CAP],
           json.dumps(ids), len(ids) if count is None else count)
    try:
        try:
            con.execute(_INSERT, row)
        except sqlite3.OperationalError:  # table missing: pre-migration DB
            con.executescript(TABLE)
            con.execute(_INSERT, row)
        con.commit()
    except Exception:
        log.exception("access-log write failed; the lookup itself succeeded")


def events_since(con, ts: float, limit: int = 200) -> list[dict]:
    """Events newer than `ts`, oldest first — the /math live view's feed.
    Queries are the user's own questions (shown in that view by design);
    fact content never travels through here, same as the rest of viz."""
    try:
        rows = con.execute(
            "SELECT ts, kind, origin, substr(query, 1, 200) AS query "
            "FROM access_log WHERE ts > ? ORDER BY ts LIMIT ?", (ts, limit))
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:  # table not created yet: no events
        return []
