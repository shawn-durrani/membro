"""The review queue grouped by hold reason, with bulk actions (#34).

The owner's complaint, twice on the record: a big pending queue where every
row needs an individual decision, even when forty rows are held for the same
cause. The contract under test:

- every /review row carries a stable reason_class derived from the reason's
  prefix convention (first flag wins for multi-flag reasons; external writes
  class as external-write; anything unrecognised is other, never a guess);
- bulk approve/dismiss act on explicit ids, owner-gated, skipping anything
  not currently pending rather than erroring - so a stale screen re-submits
  harmlessly and no automated path can approve anything (the invariant).
"""

import pytest
from fastapi.testclient import TestClient

from memory_service import api as api_mod
from memory_service.api import reason_class


def test_reason_class_truth_table():
    assert reason_class("guest-attribution: stated by guest Sam, held") == "guest-attribution"
    assert reason_class("speaker-trust: no valid src= binding") == "speaker-trust"
    assert reason_class("grounding: not supported by the excerpt") == "grounding"
    # multi-flag reasons: the first flag is the class
    assert reason_class("speaker-trust: x; grounding: y — review before trusting") == "speaker-trust"
    assert reason_class("external write (mcp:tool) — held for review") == "external-write"
    assert reason_class("free-text with no prefix at all") == "other"
    assert reason_class("") == "other"
    assert reason_class(None) == "other"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DATA_DIR", str(tmp_path / "data"))
    app = api_mod.create_app()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.app = app
        yield c


def _admin(c):
    return {"Authorization": f"Bearer {c.app.state.admin_token}"}


def _hold_fact(c, content, reason):
    """Seed a held fact the way the MINER does - through the ledger with an
    explicit reason - because the public /facts API deliberately assigns its
    own untrusted-origin reason and would flatten every class to
    external-write."""
    from memory_service import db as mdb, ledger
    con = mdb.connect(c.app.state.settings.db_path)
    try:
        row = ledger.add_fact(con, content, c.app.state.settings,
                              origin_agent="miner-test",
                              quarantine_reason=reason)
        return row["id"]
    finally:
        con.close()


def test_review_rows_carry_reason_class(client):
    _hold_fact(client, "guest fact one here", "guest-attribution: stated by guest Sam")
    _hold_fact(client, "unbound fact two here", "speaker-trust: no valid src= binding")
    rows = client.get("/v1/review", headers=_admin(client)).json()["facts"]
    classes = sorted(r["reason_class"] for r in rows)
    assert classes == ["guest-attribution", "speaker-trust"]


def test_bulk_approve_and_dismiss_by_explicit_ids(client):
    a = _hold_fact(client, "held fact aaa content", "guest-attribution: stated by guest Sam")
    b = _hold_fact(client, "held fact bbb content", "guest-attribution: stated by guest Sam")
    d = _hold_fact(client, "held fact ddd content", "speaker-trust: no valid src= binding")

    r = client.post("/v1/facts/bulk-approve", headers=_admin(client),
                    json={"ids": [a, b, 999999]})
    assert r.json() == {"approved": 2, "skipped": 1}
    r = client.post("/v1/facts/bulk-dismiss", headers=_admin(client),
                    json={"ids": [d, a]})          # a is no longer pending
    assert r.json() == {"dismissed": 1, "skipped": 1}
    assert client.get("/v1/review", headers=_admin(client)).json()["facts"] == []
    # re-submitting the same ids is a harmless no-op, not an error
    r = client.post("/v1/facts/bulk-dismiss", headers=_admin(client),
                    json={"ids": [a, b, d]})
    assert r.status_code == 200 and r.json()["dismissed"] == 0


def test_bulk_endpoints_are_owner_gated(client):
    assert client.post("/v1/facts/bulk-approve",
                       json={"ids": [1]}).status_code in (401, 403)
    assert client.post("/v1/facts/bulk-dismiss",
                       json={"ids": [1]}).status_code in (401, 403)
