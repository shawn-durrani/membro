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

    # This is the silent-failure symptom: no exception, just zero hits for
    # content that is verifiably still in `messages`.
    assert episodic.search(con, "Initech") == []

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
