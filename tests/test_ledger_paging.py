"""The ledger pages past 1,000 facts (#190).

The admin page's Load more used to ask for a bigger page on each press, and
`GET /v1/facts` refuses a page over 1,000, so the fifth press failed. It now
asks for the page after the last fact on screen with `before`, a fact id.
Pinned here: the cursor walks every fact exactly once, newest first, a save
between pages doesn't shift the next one, an id lookup ignores it, and the
page uses it instead of growing `limit`.
"""

import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memory_service import db as mdb
from memory_service import ledger
from memory_service.api import create_app

PAGE = Path(__file__).resolve().parent.parent / "memory_service" / "static" / "index.html"


@pytest.fixture
def client(settings):
    app = create_app(settings)
    return TestClient(app, base_url="http://127.0.0.1",
                      headers={"Authorization": f"Bearer {app.state.admin_token}"})


def _seed(settings, n):
    """n plain facts, written straight to the table: the paging is under
    test, not the write path, and a thousand embed threads would be slow."""
    con = mdb.connect(settings.db_path)
    now = time.time()
    con.executemany(
        "INSERT INTO facts(content, source, origin_agent, created_at, "
        "event_date, confidence, content_hash) VALUES(?,?,?,?,?,?,?)",
        [(f"Alex noted item {i} in the shed.", "user", "user", now, now,
          "high", f"h{i}") for i in range(n)])
    con.commit()
    con.close()


def _walk(client, page=200, **params):
    seen, before = [], None
    while True:
        q = {"limit": page, **params}
        if before is not None:
            q["before"] = before
        r = client.get("/v1/facts", params=q)
        assert r.status_code == 200, r.text
        rows = r.json()["facts"]
        seen += [f["id"] for f in rows]
        if len(rows) < page:
            return seen
        before = rows[-1]["id"]


def test_the_cursor_walks_past_a_thousand_facts_once_each(client, settings):
    _seed(settings, 1205)
    ids = _walk(client)
    assert len(ids) == 1205 == len(set(ids))
    assert ids == sorted(ids, reverse=True)
    # the old way stops here: a page that grows past the cap is refused
    assert client.get("/v1/facts", params={"limit": 1200}).status_code == 422


def test_a_fact_saved_between_pages_does_not_shift_the_next(client, settings,
                                                            con):
    _seed(settings, 10)
    first = client.get("/v1/facts", params={"limit": 4}).json()["facts"]
    ledger.add_fact(con, "Alex bought a new chisel at AcmeCo.", settings)
    nxt = client.get("/v1/facts", params={
        "limit": 4, "before": first[-1]["id"]}).json()["facts"]
    assert [f["id"] for f in nxt] == [first[-1]["id"] - k for k in (1, 2, 3, 4)]


def test_the_cursor_respects_status_and_search(con, settings):
    a = ledger.add_fact(con, "Alex keeps bees in Fairhaven.", settings)["id"]
    b = ledger.add_fact(con, "Alex keeps chickens too.", settings)["id"]
    c = ledger.add_fact(con, "Alex keeps a held thing.", settings,
                        quarantine_reason="test hold")["id"]
    d = ledger.add_fact(con, "Alex keeps goats now.", settings)["id"]
    assert [f["id"] for f in ledger.list_facts(con, before=d)] == [b, a]
    assert [f["id"] for f in ledger.list_facts(
        con, status="quarantined", before=d)] == [c]
    assert [f["id"] for f in ledger.list_facts(
        con, query="bees", before=d)] == [a]
    assert ledger.list_facts(con, before=a) == []


def test_an_id_lookup_ignores_the_cursor(client, con, settings):
    ids = [ledger.add_fact(con, f"Alex fact number {i} here.", settings)["id"]
           for i in range(3)]
    r = client.get("/v1/facts", params={
        "q": f"#{ids[0]},{ids[2]}", "before": ids[0]}).json()["facts"]
    assert sorted(f["id"] for f in r) == [ids[0], ids[2]]


def test_before_must_be_a_fact_id(client):
    assert client.get("/v1/facts", params={"before": 0}).status_code == 422
    assert client.get("/v1/facts", params={"before": "x"}).status_code == 422


def test_the_admin_page_pages_with_the_cursor():
    js = PAGE.read_text()
    body = js[js.index("async function loadLedger"):]
    body = body[:body.index("\nfunction ")]
    assert 'params.set("before"' in body
    assert not re.search(r"limit:\s*String\([^)]*\+", body)   # no growing page
