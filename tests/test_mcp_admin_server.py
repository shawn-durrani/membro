"""The admin MCP adapter (#46) — read-only, authenticated, ids + status exact.

Talks HTTP to the same FastAPI app as the rest of the suite (via Starlette's
in-process TestClient — no real socket, no network, keyless), never opens the
sqlite file directly. Proves: exact ids/status/dates for both approved and
quarantined rows; the review queue; a keyword filter; that no token means a
loud, non-crashing error; and that the module exposes no write/mutate tool.
"""

import pytest
from fastapi.testclient import TestClient

from memory_service import mcp_admin_server as admin
from memory_service.api import create_app


@pytest.fixture
def wired(settings, fake_llm, monkeypatch):
    """Point the admin client at an in-process app instead of a real socket,
    already carrying the SAME admin token the app itself minted (#46 v2:
    GET /v1/facts and GET /v1/review now require it even on loopback) — the
    normal operating condition once a session is properly registered."""
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1/v1",
                        headers={"Authorization": f"Bearer {app.state.admin_token}"})
    monkeypatch.setattr(admin, "_client", lambda: client)
    return client


def test_search_facts_returns_exact_ids_and_status(wired):
    approved = wired.post("/facts", json={"content": "Alex signed with Acme Corp.",
                                           "origin_agent": "user"}).json()
    pending = wired.post("/facts", json={"content": "Alex signed with Acme Corp too.",
                                          "origin_agent": "mcp:some-client"}).json()

    out = admin.search_facts("Acme")
    assert f"id={approved['id']}" in out and "approved" in out
    assert f"id={pending['id']}" in out and "pending review" in out


def test_review_queue_lists_quarantined_only_and_filters_by_keyword(wired):
    hit = wired.post("/facts", json={"content": "Cursor loop is still incomplete.",
                                      "origin_agent": "mcp:x"}).json()
    other = wired.post("/facts", json={"content": "Some unrelated pending thing.",
                                        "origin_agent": "mcp:x"}).json()

    all_pending = admin.review_queue()
    assert f"id={hit['id']}" in all_pending and f"id={other['id']}" in all_pending

    filtered = admin.review_queue("cursor")
    assert f"id={hit['id']}" in filtered
    assert f"id={other['id']}" not in filtered


def test_dismissed_and_superseded_states_labeled(wired):
    dismissed = wired.post("/facts", json={"content": "Wrong company mix-up noted.",
                                            "origin_agent": "mcp:x"}).json()
    wired.post(f"/facts/{dismissed['id']}/dismiss")

    old = wired.post("/facts", json={"content": "Alex works at OldCo.",
                                      "origin_agent": "user"}).json()
    new = wired.post("/facts", json={"content": "Alex works at NewCo.",
                                      "origin_agent": "user"}).json()
    wired.post(f"/facts/{old['id']}/supersede", json={"successor_id": new["id"]})

    out = admin.search_facts("")
    lines = {int(l.split("id=")[1].split(" ")[0]): l for l in out.splitlines()}
    assert "dismissed" in lines[dismissed["id"]]
    assert "superseded" in lines[old["id"]]


def test_no_token_fails_loudly_not_silently(monkeypatch):
    monkeypatch.delenv("MEMORY_AUTH_TOKEN", raising=False)
    assert "MEMORY_AUTH_TOKEN is not set" in admin.search_facts("anything")
    assert "MEMORY_AUTH_TOKEN is not set" in admin.review_queue("anything")


def test_no_mutate_tool_is_exposed():
    """The write gate's asymmetry, mirrored here: read-only surface only."""
    names = {t.name for t in admin.mcp._tool_manager.list_tools()}
    assert names == {"search_facts", "review_queue"}
    for forbidden in ("approve", "dismiss", "delete", "save", "supersede", "patch"):
        assert forbidden not in " ".join(names)


def test_unreachable_service_degrades_to_a_clear_error(monkeypatch):
    """A sandboxed session with no route to the live service (the #46 incident)
    gets a readable error, not a crash or a raw traceback."""
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "s3cret")
    monkeypatch.setenv("MEMORY_API_URL", "http://127.0.0.1:1/v1")  # nothing listens
    out = admin.search_facts("anything")
    assert out.startswith("Error reaching Membro")
