"""The MCP recall tool returns current facts only (#191).

`recall_memory` never asks recall for superseded facts, the same default as
HTTP `/v1/recall`, and docs/API.md says so. Its formatter used to carry a
marker for superseded history that could never fire, and it was removed.
Pinned here: a superseded fact stays out on both recall paths, its successor
comes back, and nothing in the output dresses a current fact as history.
"""

import pytest

from memory_service import ledger, mcp_server


@pytest.fixture(autouse=True)
def _wire_mcp_server(settings, monkeypatch):
    monkeypatch.setattr(mcp_server, "SETTINGS", settings)


def test_a_superseded_fact_never_comes_back(con, settings):
    old = ledger.add_fact(con, "Alex lives in Springfield.", settings)["id"]
    new = ledger.add_fact(con, "Alex lives in Fairhaven now.", settings)["id"]
    ledger.mark_superseded(con, old, new)
    for query in ("", "Alex lives"):
        out = mcp_server.recall_memory(query)
        assert "Fairhaven" in out
        assert "Springfield" not in out
        assert "SUPERSEDED" not in out


def test_a_low_confidence_fact_still_reads_as_uncertain(con, settings):
    ledger.add_fact(con, "Alex might take up pottery.", settings,
                    confidence="low", event_date=1767225600.0)
    out = mcp_server.recall_memory("")
    assert "?" in out.split("]")[0] and "pottery" in out
