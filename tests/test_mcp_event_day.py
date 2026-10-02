"""The MCP save keeps event_date as a calendar day, like the HTTP save (#192).

Contract 1.4 made `event_date` a calendar day at the owner's local midnight,
so two facts about one day compare equal and recall breaks the tie on the
save time. `POST /v1/facts` rounded and `save_memory` didn't, so a fact
saved over MCP with a time of day sorted by clock time instead.
"""

import datetime

import pytest

from memory_service import db, ledger, mcp_server


@pytest.fixture(autouse=True)
def _wire_mcp_server(settings, monkeypatch):
    monkeypatch.setattr(mcp_server, "SETTINGS", settings)


def _saved(con):
    return ledger.review_queue(con)[0]["event_date"]


@pytest.mark.parametrize("sent", ["2026-03-14", "2026-03-14T18:45:00",
                                  "2026-03-14T06:05:30.250"])
def test_a_date_is_kept_as_its_local_midnight(con, sent):
    mcp_server.save_memory("Alex finished building a workbench.", sent)
    midnight = datetime.datetime(2026, 3, 14).timestamp()
    assert _saved(con) == midnight


def test_a_timestamp_with_an_offset_keeps_the_local_day_it_falls_on(con):
    sent = "2026-03-14T23:30:00+00:00"
    mcp_server.save_memory("Alex finished building a workbench.", sent)
    ts = datetime.datetime.fromisoformat(sent).timestamp()
    assert _saved(con) == db.day_start(ts)


def test_an_unreadable_date_is_ignored_not_refused(con):
    out = mcp_server.save_memory("Alex finished building a workbench.",
                                 "the fourteenth of March")
    assert "held for the user's review" in out
    today = db.day_start(db.now())
    assert _saved(con) == today
