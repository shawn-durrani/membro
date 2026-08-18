"""FTS5 external-content indexes (messages_fts, attachments_fts) are only
kept current by an AFTER INSERT trigger on their base table — nothing
backfills them from existing rows. These tests guard the failure mode
diagnosed as the cause of search_history returning zero results for every
query: the index silently going out of sync with its base table (e.g. a
dropped/recreated virtual table), which MATCH queries survive without
raising, so callers never learn anything is wrong."""

from memory_service import db, episodic


def test_fts_tracks_messages_on_ingest(con):
    """The ordinary path: ingest keeps messages_fts row-for-row with
    messages via the trigger, with no separate step required."""
    status = db.fts_status(con)
    assert status["messages_fts"]["base_rows"] == 0
    assert status["messages_fts"]["fts_rows"] == 0
    assert status["messages_fts"]["in_sync"] is True

    episodic.ingest(con, "multi-model-chat", "chat-1", [
        {"external_id": "m1", "speaker": "user", "content": "an issue to track",
         "created_at": 1700000000.0},
        {"external_id": "m2", "speaker": "claude", "content": "a follow-up TODO",
         "created_at": 1700000060.0},
    ])

    status = db.fts_status(con)
    assert status["messages_fts"]["base_rows"] == 2
    assert status["messages_fts"]["fts_rows"] == 2
    assert status["messages_fts"]["in_sync"] is True
    assert episodic.search(con, "issue")
    assert episodic.search(con, "TODO")


def test_repair_fts_fixes_deliberately_emptied_index(con, sample_conversation):
    """Simulate the diagnosed desync directly (drop + recreate the virtual
    table empty, the same state a schema mishap or partial restore would
    leave), then confirm search is silently broken, then confirm repair_fts
    fixes it without touching `messages` itself."""
    messages_before = [dict(r) for r in con.execute(
        "SELECT * FROM messages ORDER BY id")]
    assert messages_before  # sample_conversation ingested rows

    # Reproduce the desync: drop and recreate the FTS shadow table empty,
    # exactly what CREATE VIRTUAL TABLE IF NOT EXISTS leaves behind if the
    # table was ever dropped after messages already existed.
    con.execute("DROP TABLE messages_fts")
    con.execute("CREATE VIRTUAL TABLE messages_fts USING fts5("
                "content, content='messages', content_rowid='id')")
    con.commit()

    status = db.fts_status(con)
    assert status["messages_fts"]["base_rows"] > 0
    assert status["messages_fts"]["fts_rows"] == 0
    assert status["messages_fts"]["in_sync"] is False

    # The silent-failure symptom used to be zero hits with no exception.
    # Since the query-path guard (#38), a drifted-EMPTY index falls back to
    # the bounded LIKE path, so the content is still findable pre-repair.
    drifted_hits = episodic.search(con, "Initech")
    assert drifted_hits and any("Initech" in h["content"] for h in drifted_hits)

    result = db.repair_fts(con)
    assert result["repaired"] == ["messages_fts"]

    status = db.fts_status(con)
    assert status["messages_fts"]["in_sync"] is True
    assert status["messages_fts"]["fts_rows"] == status["messages_fts"]["base_rows"]

    hits = episodic.search(con, "Initech")
    assert hits
    assert any("Initech" in h["content"] for h in hits)

    # The repair only touches the derived index, never the immutable
    # episodic record itself.
    messages_after = [dict(r) for r in con.execute(
        "SELECT * FROM messages ORDER BY id")]
    assert messages_after == messages_before


def test_repair_fts_is_a_noop_when_in_sync(con, sample_conversation):
    before = db.fts_status(con)
    result = db.repair_fts(con)
    assert result["repaired"] == []
    after = db.fts_status(con)
    assert before == after


def test_init_repairs_a_desynced_index_on_startup(settings):
    """db.init() runs the repair itself — a maintainer never has to know to
    run the script for the fix to take effect on next restart."""
    db.init(settings)
    con = db.connect(settings.db_path)
    try:
        episodic.ingest(con, "multi-model-chat", "chat-1", [
            {"external_id": "m1", "speaker": "user", "content": "an issue",
             "created_at": 1700000000.0},
        ])
        con.execute("DROP TABLE messages_fts")
        con.execute("CREATE VIRTUAL TABLE messages_fts USING fts5("
                    "content, content='messages', content_rowid='id')")
        con.commit()
        assert db.fts_status(con)["messages_fts"]["in_sync"] is False
    finally:
        con.close()

    db.init(settings)  # simulates a service restart

    con = db.connect(settings.db_path)
    try:
        assert db.fts_status(con)["messages_fts"]["in_sync"] is True
        assert episodic.search(con, "issue")
    finally:
        con.close()


def test_health_surfaces_fts_desync(settings):
    db.init(settings)
    con = db.connect(settings.db_path)
    try:
        episodic.ingest(con, "multi-model-chat", "chat-1", [
            {"external_id": "m1", "speaker": "user", "content": "hello",
             "created_at": 1700000000.0},
        ])
    finally:
        con.close()

    h = db.health(settings)
    assert h["fts_in_sync"] is True
    assert h["fts"]["messages_fts"]["in_sync"] is True

    con = db.connect(settings.db_path)
    try:
        con.execute("DROP TABLE messages_fts")
        con.execute("CREATE VIRTUAL TABLE messages_fts USING fts5("
                    "content, content='messages', content_rowid='id')")
        con.commit()
    finally:
        con.close()

    h = db.health(settings)
    assert h["fts_in_sync"] is False
    assert h["fts"]["messages_fts"]["in_sync"] is False


def test_search_falls_back_when_the_index_is_empty_but_valid(
        con, sample_conversation):
    """#38: an empty-but-syntactically-valid index answers MATCH with zero
    rows without raising, so the exception-only fallback never ran and drift
    read as a genuine no-match. The query path now probes for exactly that
    shape and serves the bounded LIKE fallback."""
    con.execute("DROP TABLE messages_fts")
    con.execute("CREATE VIRTUAL TABLE messages_fts USING fts5("
                "content, content='messages', content_rowid='id')")
    con.commit()
    hits = episodic.search(con, "Initech")
    assert hits and any("Initech" in h["content"] for h in hits)
    # and a word genuinely absent from the record still finds nothing -
    # the fallback is bounded, not a different answer
    assert episodic.search(con, "Hooli") == []


def test_healthy_no_match_never_falls_back(con, sample_conversation):
    """An ordinary no-match on a HEALTHY index stays an honest empty result:
    the drift probe sees a non-empty index and stops - no LIKE scan."""
    assert db.fts_status(con)["messages_fts"]["in_sync"] is True
    assert episodic.search(con, "Hooli") == []


def test_empty_record_searches_empty(settings):
    """A fresh, genuinely empty store: zero rows everywhere is not drift."""
    db.init(settings)
    con = db.connect(settings.db_path)
    try:
        assert episodic.search(con, "anything") == []
    finally:
        con.close()
