"""The three human erasers, in one place (#101).

The owner's danger-zone routes and the snapshot restore both erase through
these functions, so a replay after a restore removes exactly what the
owner's hand removed: the row, its search-index entry, and for a message
the quarantine of the facts mined from it. Every eraser journals a
content-free tombstone in the same transaction (#45). Each returns None when
the row is not there, which a route turns into a 404 and a replay counts as
already absent.
"""

import time

from . import db


def erase_fact(con, fact_id: int, journal_ts: float | None = None) -> dict | None:
    cur = con.execute("DELETE FROM facts WHERE id=?", (fact_id,))
    if not cur.rowcount:
        return None
    db.journal_erasure(con, "fact", f"fact:{fact_id}", ts=journal_ts)
    con.commit()
    return {"deleted": fact_id}


def erase_attachment(con, settings, att_id: int,
                     journal_ts: float | None = None) -> dict | None:
    """Row, FTS tombstone, journal; the file itself is unlinked only when no
    other row references the same content-addressed bytes."""
    row = con.execute("SELECT stored_name, extracted_text FROM attachments "
                      "WHERE id=?", (att_id,)).fetchone()
    if not row:
        return None
    # external-content FTS needs an explicit tombstone before the row goes
    con.execute("INSERT INTO attachments_fts(attachments_fts, rowid, "
                "extracted_text) VALUES('delete', ?, ?)",
                (att_id, row["extracted_text"]))
    con.execute("DELETE FROM attachments WHERE id=?", (att_id,))
    shared = con.execute("SELECT 1 FROM attachments WHERE stored_name=? LIMIT 1",
                         (row["stored_name"],)).fetchone()
    db.journal_erasure(con, "attachment", f"attachment:{att_id}", ts=journal_ts)
    con.commit()
    removed = False
    if not shared:
        (settings.data_dir / "attachments" / row["stored_name"]).unlink(missing_ok=True)
        removed = True
    return {"deleted": att_id, "file_removed": removed}


def erase_message(con, message_id: int, journal_ts: float | None = None) -> dict | None:
    """Row and FTS tombstone; live facts mined from it quarantine with a
    source-deleted reason rather than vanishing; attachments that rode the
    message keep their own eraser and are only counted."""
    row = con.execute(
        "SELECT m.id, m.external_id, m.content, m.conversation_id, "
        "cv.source_app, cv.external_id AS conv_ref "
        "FROM messages m JOIN conversations cv "
        "ON cv.id=m.conversation_id WHERE m.id=?",
        (message_id,)).fetchone()
    if not row:
        return None
    con.execute("INSERT INTO messages_fts(messages_fts, rowid, content) "
                "VALUES('delete', ?, ?)", (message_id, row["content"]))
    con.execute("DELETE FROM messages WHERE id=?", (message_id,))
    held = con.execute(
        "UPDATE facts SET quarantined_at=?, quarantine_reason=? "
        "WHERE source_message_id=? "
        "AND invalidated_at IS NULL AND quarantined_at IS NULL",
        (time.time(), "source-deleted: origin message erased by owner",
         message_id)).rowcount
    kept = con.execute(
        "SELECT COUNT(*) AS n FROM attachments "
        "WHERE conversation_id=? AND message_external_id=?",
        (row["conversation_id"], row["external_id"])).fetchone()["n"]
    db.journal_erasure(
        con, "message",
        f"message:{message_id} conv:{row['source_app']}/{row['conv_ref']} "
        f"ref:{row['external_id']}", ts=journal_ts)
    con.commit()
    return {"deleted": message_id, "facts_held": held, "attachments_kept": kept}
