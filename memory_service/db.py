"""SQLite layer — schema v1, connections, backups, health.

Invariants live here as much as in the callers: `facts` has no automated DELETE
path (only api.py's human-initiated endpoint issues one), and nothing in this
module ever rewrites `messages` content after insert.
"""

import hashlib
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("memory_service.db")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'user',        -- user | chat | model | synthesis
  origin_agent TEXT NOT NULL DEFAULT 'user',  -- who authored it (proof, not trust)
  conversation_id INTEGER,
  source_message_id INTEGER,
  created_at REAL NOT NULL,
  event_date REAL NOT NULL,                   -- the date the fact is ABOUT; never null
  confidence TEXT NOT NULL DEFAULT 'high',
  importance INTEGER,                          -- 1-10, LLM-scored at extraction (weighting)
  content_hash TEXT NOT NULL,
  embedding BLOB,
  invalidated_at REAL,                         -- temporal validity: superseded, never deleted
  superseded_by INTEGER,
  quarantined_at REAL,                         -- held out of recall+summary pending review
  quarantine_reason TEXT,
  review_dismissed_at REAL,                    -- reviewed-and-kept-out (stays quarantined)
  person_id INTEGER                            -- #33: the person record this fact links to
);

CREATE TABLE IF NOT EXISTS conversations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_app TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  mined_upto INTEGER NOT NULL DEFAULT 0,       -- watermark: last message id distilled
  created_at REAL NOT NULL,
  UNIQUE(source_app, external_id)
);

CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  external_id TEXT NOT NULL,
  speaker TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL,
  speaker_identity TEXT NOT NULL DEFAULT '',  -- #33 contract 1.2: {person, confidence, method} json, or ''
  web_sources TEXT NOT NULL DEFAULT '',       -- #55 contract 1.3: json list of web domains the turn drew on, or ''
  UNIQUE(conversation_id, external_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content, content='messages', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

-- Attachments: files that traveled with messages (pasted documents, PDFs,
-- images). Part of the episodic record — append-only, immutable after insert,
-- never deleted by software. Bytes live content-addressed on disk
-- (data/attachments/<sha256>); extracted text is FTS-searchable.
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  message_external_id TEXT NOT NULL DEFAULT '',
  filename TEXT NOT NULL,
  mime TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  stored_name TEXT NOT NULL,
  extracted_text TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  UNIQUE(conversation_id, message_external_id, sha256)
);

CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5(
  extracted_text, content='attachments', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS attachments_ai AFTER INSERT ON attachments BEGIN
  INSERT INTO attachments_fts(rowid, extracted_text) VALUES (new.id, new.extracted_text);
END;

CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

-- Summary versions: every generated profile, append-only — regenerating never
-- destroys the previous profile, and any version can be restored (a restore
-- appends a new row pointing at its source; history is never rewritten).
CREATE TABLE IF NOT EXISTS summary_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  generated_at REAL NOT NULL,
  content TEXT NOT NULL,
  source_fact_ids TEXT NOT NULL DEFAULT '[]',
  word_count INTEGER NOT NULL DEFAULT 0,
  word_budget INTEGER,
  model TEXT,
  restored_from INTEGER                      -- non-null: human restore of that version
);

-- The access log: every lookup (recall / search / summary fetch), append-only.
-- Additive to schema v1 — created on init for the service, lazily by
-- access.record for MCP adapter processes that reach an older DB first.
CREATE TABLE IF NOT EXISTS access_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,                     -- recall | search | summary
  origin TEXT NOT NULL DEFAULT 'http',    -- http | mcp:<client-name>
  query TEXT NOT NULL DEFAULT '',
  result_ids TEXT NOT NULL DEFAULT '[]',  -- facts returned: [[fact_id, score|null], ...]
  result_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_access_log_ts ON access_log(ts);

-- Machine-generated image descriptions (#107). Their OWN append-only table on
-- purpose: the attachments row is part of the episodic record and no module
-- may UPDATE it (tested) — new derived information gets appended alongside,
-- never written into the original. PRIMARY KEY = one caption per attachment,
-- forever; ON DELETE CASCADE rides the human-initiated eraser only (the sole
-- sanctioned attachment delete). The trigger swaps the attachment's empty FTS
-- entry for the caption text so /v1/search sees what the photo showed.
CREATE TABLE IF NOT EXISTS attachment_captions(
  attachment_id INTEGER PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
  caption TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS attachment_captions_ai AFTER INSERT ON attachment_captions BEGIN
  INSERT INTO attachments_fts(attachments_fts, rowid, extracted_text)
    VALUES('delete', new.attachment_id,
           (SELECT extracted_text FROM attachments WHERE id = new.attachment_id));
  INSERT INTO attachments_fts(rowid, extracted_text) VALUES (new.attachment_id, new.caption);
END;

-- Person records (#33): the fleet's identity home. Apps capture voices
-- and assert identity; membro records it durably. A person row is never
-- deleted - forgetting marks it (forgotten_at) and deletes only the audio.
CREATE TABLE IF NOT EXISTS persons(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,          -- stable id other apps use, e.g. 'p-9f3a2c'
  display_name TEXT NOT NULL,
  name_owner_set INTEGER NOT NULL DEFAULT 0,  -- owner renamed it: client upserts can't change it
  relationship TEXT NOT NULL DEFAULT '',      -- owner-set free text ('partner', 'colleague')
  created_at REAL NOT NULL,
  origin_client TEXT NOT NULL DEFAULT '',     -- which app first created it
  updated_at REAL NOT NULL DEFAULT 0, -- stamped on every change; what ?since= filters on
  merged_into INTEGER,                -- set when merged into another person; row kept
  forgotten_at REAL                   -- set when forgotten; how other apps find out
);

CREATE TABLE IF NOT EXISTS person_aliases(
  person_id INTEGER NOT NULL REFERENCES persons(id),
  alias TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- an alias points at exactly one person
  kind TEXT NOT NULL DEFAULT 'spelling'       -- spelling | pronunciation
);

CREATE TABLE IF NOT EXISTS voice_anchors(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES persons(id),
  sha256 TEXT NOT NULL,               -- file fingerprint; the same clip is never stored twice
  stored_name TEXT NOT NULL,          -- file on disk under voice_anchors/, owner-only
  seconds REAL NOT NULL DEFAULT 0,
  score REAL NOT NULL DEFAULT 0,      -- the uploading app's quality score, kept for reference
  source TEXT NOT NULL DEFAULT '',    -- accumulated | introduction | correction
  captured_at REAL NOT NULL DEFAULT 0,
  client TEXT NOT NULL DEFAULT '',
  UNIQUE(person_id, sha256)
);

-- The erasure journal (#45): each use of the three human erasers (fact /
-- attachment / message) appends one CONTENT-FREE row - kind, refs/ids, when.
-- What was erased is gone; THAT it was erased is not. Additive to schema v1,
-- created on init like access_log.
CREATE TABLE IF NOT EXISTS erasures(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,                     -- fact | attachment | message
  ref TEXT NOT NULL                       -- ids only, never content
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def journal_erasure(con, kind: str, ref: str) -> None:
    """One content-free row per human erasure - ids in `ref`, never content.
    Caller commits: the journal row must land in the SAME transaction as the
    delete it records."""
    con.execute("INSERT INTO erasures(ts, kind, ref) VALUES (?,?,?)",
                (time.time(), kind, ref))


def init(settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.data_dir, 0o700)
    if settings.db_path.exists():
        backup(settings)  # pre-migration restore point, every startup
    con = connect(settings.db_path)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        if ver > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{ver} is newer than this code (v{SCHEMA_VERSION}) — refusing to open")
        con.executescript(SCHEMA)
        # #33 additive column on an existing DB: IF NOT EXISTS cannot add
        # columns, so guard an ALTER by inspecting the live table.
        cols = {r[1] for r in con.execute("PRAGMA table_info(facts)")}
        if "person_id" not in cols:
            con.execute("ALTER TABLE facts ADD COLUMN person_id INTEGER")
        mcols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        if "speaker_identity" not in mcols:
            con.execute("ALTER TABLE messages ADD COLUMN speaker_identity "
                        "TEXT NOT NULL DEFAULT ''")
        if "web_sources" not in mcols:  # #55 additive, same pattern
            con.execute("ALTER TABLE messages ADD COLUMN web_sources "
                        "TEXT NOT NULL DEFAULT ''")
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
        # Cheap (row-count comparison) and safe (no-op unless desynced) — see
        # repair_fts. Runs every startup so a desynced search index can never
        # sit silent indefinitely; a repair is logged loudly since it means
        # search was returning zero results until now.
        repaired = repair_fts(con)
        if repaired["repaired"]:
            log.warning("startup FTS repair: rebuilt %s (was out of sync with "
                        "its base table — search was silently returning zero "
                        "results for affected data)", repaired["repaired"])
    finally:
        con.close()
    os.chmod(settings.db_path, 0o600)


# ---- FTS5 external-content sync (#issue: search returning zero results) ----
#
# messages_fts / attachments_fts are external-content FTS5 tables: they hold
# no data of their own, only a shadow index, kept current solely by the
# AFTER INSERT triggers in SCHEMA. Nothing ever backfills them from an
# existing base table. If the virtual table is ever dropped and silently
# recreated empty (schema experiment, corruption recovery, a partial
# restore that copied `messages` but not the `messages_fts` shadow tables),
# the index desyncs from its base table forever — MATCH queries then just
# return zero rows, no error, so callers never learn anything is wrong (the
# LIKE fallback in episodic.search only fires on an exception, not on an
# empty-but-successful MATCH). fts_status/repair_fts close that gap: a cheap
# row-count comparison, and the documented `('rebuild')` command to
# resync — safe to run any number of times, including when already in sync.
#
# The count has to come from the `_docsize` shadow table, NOT
# `SELECT COUNT(*) FROM <fts_table>`: for an external-content table, a
# non-MATCH query against the fts5 table itself is answered by reading
# straight through to the content table (that's the whole point of
# "external content" — no duplicate storage), so it returns the base
# table's count even when the FTS index proper is completely empty. The
# `_docsize` shadow table holds one row per rowid actually indexed by FTS5,
# so it — and only it — reflects the index's real state. (Confirmed
# empirically: dropping+recreating the virtual table left `_docsize` at 0
# while a plain COUNT(*) on the fts table still read through to the base
# table's row count.)
FTS_TABLES = (("messages", "messages_fts"), ("attachments", "attachments_fts"))


def fts_status(con: sqlite3.Connection) -> dict:
    """Row-count comparison between each base table and its external-content
    FTS5 index's `_docsize` shadow table (see note above — NOT a plain
    COUNT(*) on the fts5 table, which reads through to the base table and
    would always claim to be in sync). Tables that don't exist yet
    (older/partial schema) are skipped rather than raising."""
    status = {}
    for base, fts in FTS_TABLES:
        try:
            base_n = con.execute(f"SELECT COUNT(*) FROM {base}").fetchone()[0]
            fts_n = con.execute(
                f"SELECT COUNT(*) FROM {fts}_docsize").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        status[fts] = {"base_table": base, "base_rows": base_n,
                       "fts_rows": fts_n, "in_sync": base_n == fts_n}
    return status


def repair_fts(con: sqlite3.Connection) -> dict:
    """Idempotent: rebuild any external-content FTS5 index whose row count
    disagrees with its base table. A no-op (no writes) when everything is
    already in sync. Returns {"checked": [...], "repaired": [...]} — never
    touches `messages`/`attachments` themselves, only their derived index."""
    status = fts_status(con)
    repaired = []
    for fts, s in status.items():
        if not s["in_sync"]:
            con.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
            repaired.append(fts)
    if repaired:
        con.commit()
    return {"checked": list(status), "repaired": repaired}


def now() -> float:
    return time.time()


def content_hash(text: str) -> str:
    norm = re.sub(r"\W+", " ", text.lower()).strip()
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def get_setting(con, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------- backups ----------

def backup(settings) -> Path | None:
    """One consistent online snapshot; local rotation + optional mirror folder.
    Only completed static snapshots are mirrored — never the live WAL DB."""
    if not settings.db_path.exists():
        return None
    bdir = settings.data_dir / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / time.strftime("memory-%Y%m%d-%H%M%S.db")
    src = connect(settings.db_path)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    _rotate(bdir, settings.backup_keep)
    if settings.mirror_dir:
        try:
            Path(settings.mirror_dir).mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, Path(settings.mirror_dir) / dest.name)
            _rotate(Path(settings.mirror_dir), settings.mirror_keep)
        except OSError:
            pass  # mirror is best-effort; local snapshot already exists
    return dest


def _rotate(folder: Path, keep: int) -> None:
    snaps = sorted(p for p in folder.glob("memory-*.db"))
    for old in snaps[:-keep]:
        old.unlink(missing_ok=True)


def _changed_since_last_snapshot(settings) -> bool:
    """True when the DB (or its WAL) was written after the newest snapshot.
    Errs toward backing up: a WAL checkpoint bumps mtime without a content
    change, and that false positive just costs one extra snapshot."""
    snaps = sorted((settings.data_dir / "backups").glob("memory-*.db"))
    if not snaps:
        return True
    last = snaps[-1].stat().st_mtime
    for p in (settings.db_path, settings.db_path.with_suffix(".db-wal")):
        if p.exists() and p.stat().st_mtime > last:
            return True
    return False


def start_backup_scheduler(settings) -> threading.Event:
    """Snapshot on a timer, not just at startup — a long-running instance must
    never sit on a stale restore point. Skips ticks where nothing changed so
    rotation keeps real history depth instead of identical copies.
    Returns a stop Event; interval <= 0 disables (the Event is still returned)."""
    stop = threading.Event()
    hours = settings.backup_interval_hours
    if hours <= 0:
        return stop

    def _loop():
        while not stop.wait(hours * 3600):
            try:
                if _changed_since_last_snapshot(settings):
                    backup(settings)
            except Exception:
                log.exception("scheduled backup failed; will retry next interval")

    threading.Thread(target=_loop, daemon=True, name="backup-scheduler").start()
    return stop


# ---- integrity verdict cache (#79) ----
#
# `PRAGMA quick_check` scans the whole database file (hundreds of ms on a tens-of-MB file, growing
# with it), and /v1/health used to run it inline — which put a file-integrity
# scan on the chat client's round-critical path every time its 30s probe
# cache expired. The scan detects corruption; nothing about that detection
# requires the caller to WAIT for it. Same cadence, off the request path:
# the verdict is warmed synchronously once (app startup / first call), then
# health() always answers instantly from the cache and kicks a background
# refresh when the cached verdict is older than INTEGRITY_TTL_S. A failed
# check still surfaces — it flips the cached verdict (=> status "degraded")
# and logs at ERROR.

INTEGRITY_TTL_S = 30.0
_integrity_lock = threading.Lock()
_integrity: dict = {"verdict": None, "checked_at": 0.0, "refreshing": False}


def _refresh_integrity(settings) -> str:
    con = connect(settings.db_path)
    try:
        verdict = con.execute("PRAGMA quick_check").fetchone()[0]
    except Exception as exc:  # a scan that cannot run is itself a red flag
        verdict = f"check failed: {exc}"
    finally:
        con.close()
    with _integrity_lock:
        _integrity["verdict"] = verdict
        _integrity["checked_at"] = time.time()
    if verdict != "ok":
        log.error("database integrity check: %s", verdict)
    return verdict


def integrity_verdict(settings) -> str:
    """Last known quick_check verdict, refreshed in the background when
    stale — never blocks the caller except the very first call in a
    process (normally paid at create_app time, not by a live probe)."""
    with _integrity_lock:
        verdict = _integrity["verdict"]
        stale = time.time() - _integrity["checked_at"] > INTEGRITY_TTL_S
        should_refresh = stale and not _integrity["refreshing"]
        if should_refresh:
            _integrity["refreshing"] = True
    if verdict is None:
        try:
            return _refresh_integrity(settings)
        finally:
            with _integrity_lock:
                _integrity["refreshing"] = False
    if should_refresh:
        def _run():
            try:
                _refresh_integrity(settings)
            finally:
                with _integrity_lock:
                    _integrity["refreshing"] = False
        threading.Thread(target=_run, daemon=True,
                         name="integrity-refresh").start()
    return verdict


def health(settings) -> dict:
    con = connect(settings.db_path)
    try:
        integrity = integrity_verdict(settings)
        journal = con.execute("PRAGMA journal_mode").fetchone()[0]
        sqlite_version = con.execute("SELECT sqlite_version()").fetchone()[0]
        f = con.execute(
            "SELECT COUNT(*) total, "
            "COALESCE(SUM(invalidated_at IS NULL AND quarantined_at IS NULL),0) current, "
            "COALESCE(SUM(invalidated_at IS NOT NULL),0) superseded, "
            "COALESCE(SUM(quarantined_at IS NOT NULL),0) quarantined "
            "FROM facts").fetchone()
        messages = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conversations = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        att = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0), "
            "COALESCE(SUM(extracted_text != ''),0) FROM attachments").fetchone()
        # Surfaces the search-index-desync class of bug (previously silent:
        # an empty/short FTS index just returns zero matches, no error) as an
        # explicit, checkable field instead — see repair_fts.
        fts = fts_status(con)
    finally:
        con.close()
    bdir = settings.data_dir / "backups"
    snaps = sorted(bdir.glob("memory-*.db")) if bdir.exists() else []
    return {
        "sqlite_version": sqlite_version,
        "journal_mode": journal,
        "integrity": integrity,
        # non-contractual (rides in /v1/health's `detail`): when the cached
        # verdict was computed, so an operator can see its age (#79)
        "integrity_checked_at": _integrity["checked_at"] or None,
        "size_bytes": settings.db_path.stat().st_size if settings.db_path.exists() else 0,
        "facts": dict(f),
        "messages": messages,
        "conversations": conversations,
        "attachments": {"total": att[0], "bytes": att[1], "searchable": att[2]},
        "fts": fts,
        "fts_in_sync": all(v["in_sync"] for v in fts.values()),
        "last_backup_at": snaps[-1].stat().st_mtime if snaps else None,
        "backups_kept": len(snaps),
    }
