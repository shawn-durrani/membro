"""History search matches word endings (#152).

The benchmark case: a search for "sibling brother sister" missed "I come
from a family with 3 sisters", because the index matched whole words only.
The index now stems each word with FTS5's porter tokenizer, and a store
built before that is rebuilt at startup through the FTS repair."""

import pytest

from memory_service import db, episodic

OLD_MESSAGES_FTS = ("CREATE VIRTUAL TABLE messages_fts USING fts5("
                    "content, content='messages', content_rowid='id')")
OLD_ATTACHMENTS_FTS = ("CREATE VIRTUAL TABLE attachments_fts USING fts5("
                       "extracted_text, content='attachments', "
                       "content_rowid='id')")


def _say(con, *texts, conv="chat-w"):
    episodic.ingest(con, "multi-model-chat", conv, [
        {"external_id": f"w{i}", "speaker": "user", "content": t,
         "created_at": 1700000000.0 + i}
        for i, t in enumerate(texts, start=1)])


def _found(con, query, needle):
    """True when a hit for `query` carries `needle`, match markers aside."""
    return any(needle in h["content"].replace(">>", "").replace("<<", "")
               for h in episodic.search(con, query))


@pytest.mark.parametrize("said, query", [
    ("I come from a family with 3 sisters.", "sibling brother sister"),
    ("I've been getting into cocktails lately.", "cocktail"),
    ("My brother is two years older than me.", "brothers"),
    ("I packed a lot of shoes for my last trip.", "packing"),
    ("We're walking the coast path on Sunday.", "walked"),
    ("She runs every morning before work.", "running"),
    ("The kids played in the garden all day.", "play"),
])
def test_plural_and_tense_match(con, said, query):
    _say(con, said)
    assert _found(con, query, said[:12])


def test_unrelated_word_still_misses(con):
    _say(con, "I come from a family with 3 sisters.")
    assert episodic.search(con, "sisal") == []


def _make_old(con):
    """The index as every store before #152 built it: no tokenizer named,
    so FTS5's default, and fully in sync by row count."""
    for fts, ddl in (("messages_fts", OLD_MESSAGES_FTS),
                     ("attachments_fts", OLD_ATTACHMENTS_FTS)):
        con.execute(f"DROP TABLE {fts}")
        con.execute(ddl)
        con.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
    con.commit()


def test_old_index_is_rebuilt_at_startup(con, settings):
    _say(con, "I come from a family with 3 sisters.", "and one brother")
    _make_old(con)
    before = [dict(r) for r in con.execute("SELECT * FROM messages")]
    status = db.fts_status(con)["messages_fts"]
    assert status["tokenizer"] == "unicode61"
    assert status["fts_rows"] == status["base_rows"]
    assert status["in_sync"] is False
    assert not _found(con, "sister", "sisters")

    db.init(settings)   # startup: the FTS repair rebuilds both indexes

    con2 = db.connect(settings.db_path)
    try:
        status = db.fts_status(con2)
        assert all(s["in_sync"] and s["tokenizer"] == "porter unicode61"
                   for s in status.values())
        assert _found(con2, "sister", "sisters")
        assert [dict(r) for r in con2.execute("SELECT * FROM messages")] == before
        # New messages index through the trigger, stemmed.
        _say(con2, "My cousins are visiting.", conv="chat-x")
        assert _found(con2, "cousin", "cousins")
        # and the next startup has nothing to do
        assert db.repair_fts(con2) == {
            "checked": ["messages_fts", "attachments_fts"], "repaired": []}
    finally:
        con2.close()


def _attach(con, text="", caption=None):
    _say(con, "see the photo", conv="chat-a")
    cid = episodic.get_conversation(con, "multi-model-chat", "chat-a")["id"]
    att = con.execute(
        "INSERT INTO attachments(conversation_id, message_external_id, "
        "filename, mime, size, sha256, stored_name, extracted_text, "
        "created_at) VALUES(?, 'w1', 'shelf.png', 'image/png', 1, 'x', 'x', "
        "?, 1700000000.0)", (cid, text)).lastrowid
    if caption:
        con.execute("INSERT INTO attachment_captions(attachment_id, caption, "
                    "model, created_at) VALUES(?, ?, 'test', 1700000000.0)",
                    (att, caption))
    con.commit()


def _file_hits(con, word):
    return [r[0] for r in con.execute(
        "SELECT rowid FROM attachments_fts WHERE attachments_fts MATCH ?",
        (word,))]


def test_rebuild_keeps_image_captions_searchable(con, settings):
    # A caption is indexed in place of the attachment's empty text, and a
    # rebuild reads the attachments table, so the repair puts it back.
    _attach(con, caption="Three softwood boards beside a track saw.")
    assert _file_hits(con, "boards")
    _make_old(con)

    db.init(settings)

    con2 = db.connect(settings.db_path)
    try:
        assert _file_hits(con2, "boards")       # still there
        assert _file_hits(con2, "board")        # and stemmed
        assert db.fts_status(con2)["attachments_fts"]["in_sync"] is True
    finally:
        con2.close()


def test_attachment_text_matches_word_endings(con):
    _attach(con, text="Quotes for the kitchen benchtops, granite and oak.")
    assert _file_hits(con, "benchtop")
