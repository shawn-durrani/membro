"""The access log — reads recorded as ground truth, like writes always were.

Append-only, persistent (survives restarts), shared across processes (the MCP
adapter writes to the same table), and never able to break the lookup it
records. Feeds the /math live view; the durable substrate for query-history
replay and reinforce-on-reuse.
"""

import json
import sqlite3

from fastapi.testclient import TestClient

from memory_service import access, db as db_mod
from memory_service.api import create_app


def _client(settings):
    # Owner-authenticated: /v1/search is gated (#125) and these tests
    # exercise search recording, which is an owner action anyway.
    app = create_app(settings)
    return TestClient(app, base_url="http://127.0.0.1",
                      headers={"Authorization": f"Bearer {app.state.admin_token}"})


def test_recall_event_reaches_live_view(settings):
    with _client(settings) as client:
        assert client.get("/v1/viz/recalls?after=0").json()["events"] == []
        client.post("/v1/recall", json={"query": "anything at all", "limit": 3})
        events = client.get("/v1/viz/recalls?after=0").json()["events"]
        assert len(events) == 1
        assert events[0]["query"] == "anything at all"
        assert events[0]["kind"] == "recall" and events[0]["origin"] == "http"
        # `after` filters: nothing newer than the event itself
        newer = client.get(f"/v1/viz/recalls?after={events[0]['ts']}").json()["events"]
        assert newer == []


def test_recall_origin_field_lands_in_log(settings):
    """Ambient recalls (a client preparing context per-message) tag themselves
    origin=auto so the live view and reinforce-on-reuse can tell preparation
    apart from a model deliberately reaching in."""
    with _client(settings) as client:
        client.post("/v1/recall", json={"query": "who is alex", "limit": 3,
                                        "origin": "auto"})
        events = client.get("/v1/viz/recalls?after=0").json()["events"]
        assert events[0]["origin"] == "auto" and events[0]["kind"] == "recall"
        # garbage origins are rejected, not silently stored
        r = client.post("/v1/recall", json={"query": "x", "origin": "BAD ORIGIN!"})
        assert r.status_code == 422


def test_log_survives_restart(settings):
    """The point of persistence: a new app instance still sees old events."""
    with _client(settings) as client:
        client.post("/v1/recall", json={"query": "before the restart", "limit": 3})
    with _client(settings) as client:  # fresh process stand-in, same data dir
        events = client.get("/v1/viz/recalls?after=0").json()["events"]
        assert [e["query"] for e in events] == ["before the restart"]


def test_recall_records_returned_fact_ids_and_scores(settings):
    """Which facts came back is the raw material of reinforce-on-reuse."""
    with _client(settings) as client:
        client.post("/v1/facts", json={"content": "Alex plays chess on Sundays."})
        got = client.post("/v1/recall",
                          json={"query": "chess sundays", "limit": 5}).json()["facts"]
        assert got  # keyword path finds it keyless
    con = db_mod.connect(settings.db_path)
    try:
        row = con.execute(
            "SELECT * FROM access_log WHERE kind='recall'").fetchone()
        ids = json.loads(row["result_ids"])
        assert [i for i, _score in ids] == [f["id"] for f in got]
        assert row["result_count"] == len(got)
    finally:
        con.close()


def test_search_and_summary_events_logged(settings, sample_conversation, con):
    con.close()  # fixture opened it; the client owns the DB from here
    with _client(settings) as client:
        client.post("/v1/search", json={"query": "Initech", "limit": 5})
        client.get("/v1/summary")
        events = client.get("/v1/viz/recalls?after=0").json()["events"]
        kinds = [e["kind"] for e in events]
        assert "search" in kinds and "summary" in kinds
        search_ev = next(e for e in events if e["kind"] == "search")
        assert search_ev["query"] == "Initech"


def test_mcp_origin_lands_in_same_log(settings, con):
    """The MCP adapter is a separate process writing the same table — origin
    mcp:<client> is how its lookups become visible to the live view."""
    access.record(con, "recall", "who is alex", origin="mcp:claude-code",
                  facts=[{"id": 1, "score": 0.5}])
    events = access.events_since(con, 0)
    assert events[0]["origin"] == "mcp:claude-code"
    with _client(settings) as client:
        served = client.get("/v1/viz/recalls?after=0").json()["events"]
        assert served[0]["origin"] == "mcp:claude-code"


def test_record_creates_table_on_unmigrated_db(tmp_path):
    """An MCP process may reach a DB the service hasn't migrated yet — the
    write must succeed anyway (lazy CREATE), never crash the lookup."""
    raw = tmp_path / "old.db"
    con = sqlite3.connect(raw)
    con.row_factory = sqlite3.Row
    access.record(con, "recall", "hello", origin="mcp:x")
    assert access.events_since(con, 0)[0]["query"] == "hello"
    con.close()


def test_record_failure_never_breaks_the_lookup(con):
    """A closed/broken connection loses the log row, nothing else."""
    con.close()
    access.record(con, "recall", "still fine")  # must not raise


def test_no_automated_delete_or_update_of_access_log():
    """Append-only, same standard as facts/messages: the service source never
    deletes or rewrites an access row."""
    import pathlib
    src_dir = pathlib.Path(access.__file__).parent
    for p in src_dir.glob("*.py"):
        src = p.read_text().lower()
        assert "delete from access_log" not in src, p.name
        assert "update access_log" not in src, p.name


def test_query_capped_and_content_free(con):
    access.record(con, "recall", "x" * 2000)
    ev = access.events_since(con, 0)[0]
    assert len(ev["query"]) <= 200  # served view caps further than storage
    row = con.execute("SELECT query FROM access_log").fetchone()
    assert len(row["query"]) == access.QUERY_CAP
    # events carry ts/kind/origin/query only — never fact content or ids
    assert set(ev) == {"ts", "kind", "origin", "query"}
