"""The episodic record — verbatim transcript, ground truth.

Ingest is idempotent on (conversation, external_id). Nothing in this service
ever updates or deletes a message after insert; that immutability is what makes
a bad ledger fact detectable and traceable to its source.

Attachments are part of the record: files that traveled with messages (pasted
documents, PDFs, images) are stored whole — bytes content-addressed on disk,
text extracted where possible so search and mining see it. The point of a
local, uncapped memory is to never lose this stuff.
"""

import base64
import json
import hashlib
import io
import logging
import os

from . import db

log = logging.getLogger("memory_service.episodic")


def _attach_dir(settings):
    d = settings.data_dir / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def extract_text(data: bytes, filename: str, mime: str) -> str:
    """Best-effort text for search/mining. Text decodes whole; PDFs extract via
    pypdf when available; everything else (images, binaries) yields "" — the
    bytes are still stored whole either way."""
    name = (filename or "").lower()
    if (mime or "").startswith("text/") or name.endswith(
            (".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml",
             ".yaml", ".yml", ".log", ".html", ".py", ".js", ".ts", ".sql")):
        return data.decode("utf-8", errors="replace")
    if mime == "application/pdf" or name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        except Exception:
            log.warning("pdf text extraction failed for %s — bytes stored, "
                        "text unsearchable", filename)
            return ""
    return ""


def add_attachment(con, settings, conversation_id: int,
                   message_external_id: str, filename: str, mime: str,
                   data: bytes) -> bool:
    """Store one attachment (bytes on disk, row + extracted text in the DB).
    Content-addressed and idempotent: the same file on the same message is a
    no-op. Returns True when a new row was inserted."""
    sha = hashlib.sha256(data).hexdigest()
    ext = os.path.splitext(filename or "")[1][:12]
    stored = f"{sha}{ext}"
    path = _attach_dir(settings) / stored
    if not path.exists():
        path.write_bytes(data)
        os.chmod(path, 0o600)
    cur = con.execute(
        "INSERT OR IGNORE INTO attachments(conversation_id, message_external_id, "
        "filename, mime, size, sha256, stored_name, extracted_text, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (conversation_id, message_external_id, filename or stored, mime or "",
         len(data), sha, stored, extract_text(data, filename, mime), db.now()))
    return bool(cur.rowcount)


def attachments_for_conversation(con, conversation_id: int) -> dict[str, list[dict]]:
    """message_external_id -> attachment rows (no bytes), for the mining view."""
    out: dict[str, list[dict]] = {}
    for r in con.execute(
            # A machine caption (#107) stands in for extracted text when the
            # attachment has none (images): the attachments row itself is never
            # updated, so the overlay happens here, at read time.
            "SELECT a.message_external_id, a.filename, a.mime, "
            "COALESCE(NULLIF(a.extracted_text,''), c.caption, '') AS extracted_text "
            "FROM attachments a "
            "LEFT JOIN attachment_captions c ON c.attachment_id = a.id "
            "WHERE a.conversation_id=? ORDER BY a.id",
            (conversation_id,)):
        out.setdefault(r["message_external_id"], []).append(dict(r))
    return out


def ingest(con, source_app: str, conversation_external_id: str,
           messages: list[dict], title: str = "", settings=None) -> dict:
    row = con.execute(
        "SELECT id FROM conversations WHERE source_app=? AND external_id=?",
        (source_app, conversation_external_id)).fetchone()
    if row:
        conv_id = row["id"]
        if title:
            con.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
    else:
        conv_id = con.execute(
            "INSERT INTO conversations(source_app, external_id, title, created_at) "
            "VALUES(?,?,?,?)",
            (source_app, conversation_external_id, title, db.now())).lastrowid
    ingested = skipped = attached = 0
    for m in messages:
        identity = m.get("speaker_identity")
        webs = m.get("web_sources") or []
        cur = con.execute(
            "INSERT OR IGNORE INTO messages(conversation_id, external_id, speaker, "
            "content, created_at, speaker_identity, web_sources) VALUES(?,?,?,?,?,?,?)",
            (conv_id, m["external_id"], m["speaker"], m["content"], m["created_at"],
             json.dumps(identity) if identity else "",
             json.dumps(webs) if webs else ""))
        if cur.rowcount:
            ingested += 1
        else:
            skipped += 1
        # Attachments attach even to already-ingested (skipped) messages — that
        # makes attachment backfill for old conversations a plain re-ingest.
        for a in m.get("attachments") or []:
            if settings is None:
                break
            try:
                data = base64.b64decode(a["data_b64"])
            except Exception:
                continue  # malformed payload: skip this file, keep the rest
            if add_attachment(con, settings, conv_id, m["external_id"],
                              a.get("filename", ""), a.get("mime", ""), data):
                attached += 1
    con.commit()
    return {"conversation_id": conv_id, "ingested": ingested,
            "skipped": skipped, "attached": attached}


def get_conversation(con, source_app: str, external_id: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM conversations WHERE source_app=? AND external_id=?",
        (source_app, external_id)).fetchone()
    return dict(row) if row else None


def messages_after(con, conversation_id: int, after_id: int = 0) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM messages WHERE conversation_id=? AND id>? ORDER BY id",
        (conversation_id, after_id))]


def search(con, query: str, limit: int = 20) -> list[dict]:
    """FTS5 over the whole record — messages AND attachment text; LIKE fallback
    for FTS syntax edge cases. Attachment hits are labeled `file: <name>` in
    the speaker slot so callers can render them without a schema change."""
    words = [w for w in (query or "").split() if w.strip()]
    if not words:
        return []
    match = " OR ".join(f'"{w}"' for w in words)
    try:
        rows = con.execute(
            "SELECT m.speaker, m.created_at, c.title, c.external_id conversation_id, "
            "snippet(messages_fts, 0, '>>', '<<', ' … ', 24) AS content "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?", (match, limit))
        hits = [dict(r) for r in rows]
        if len(hits) < limit:
            arows = con.execute(
                "SELECT 'file: ' || a.filename AS speaker, a.created_at, c.title, "
                "c.external_id conversation_id, "
                "snippet(attachments_fts, 0, '>>', '<<', ' … ', 24) AS content "
                "FROM attachments_fts JOIN attachments a ON a.id = attachments_fts.rowid "
                "JOIN conversations c ON c.id = a.conversation_id "
                "WHERE attachments_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit - len(hits)))
            hits += [dict(r) for r in arows]
        return hits
    except Exception:
        rows = con.execute(
            "SELECT m.speaker, m.created_at, c.title, c.external_id conversation_id, "
            "substr(m.content, 1, 200) AS content "
            "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.content LIKE ? ORDER BY m.id DESC LIMIT ?",
            (f"%{words[0]}%", limit))
        return [dict(r) for r in rows]
