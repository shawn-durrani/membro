"""One-off importer: a claude.ai data export → this service's episodic record.

Reads the unzipped directory that claude.ai's "Export data" produces
(conversations.json, projects/*.json, memories.json, users.json) and lands it
in the episodic record verbatim: conversations and messages with their export
timestamps, per-message attachment text, artifact contents, and project docs
all searchable. Episodic ONLY — no fact mining, no LLM calls, no ledger
writes. Old history must not enter the ledger as if its claims were current;
if some of it deserves facts, distill selectively later (POST /v1/distill).

What the export does NOT contain can't be imported: uploaded file bytes
(images, originals of PDFs) ship as names only — they land as [file: ...]
markers so the record at least shows they existed.

Usage:
  .venv/bin/python scripts/import_claude_export.py --export-dir /path/to/export \
      [--data-dir /path/to/data] [--source-app claude.ai]

Idempotent-ish: ingest dedupes messages on their export uuid, and a per-account
settings marker refuses a repeat of the same account's export while still
allowing a different account's export into the same record later.
Run against a stopped/throwaway data dir, never a live service.
"""

import argparse
import base64
import json
import socket
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_service import db, episodic  # noqa: E402
from memory_service.config import load_settings  # noqa: E402

MARKER_PREFIX = "claude_export_done"
SPEAKER = {"human": "user", "assistant": "claude"}
SERVICE_PORT = 8901


def iso_to_epoch(s: str | None, fallback: float | None = None) -> float:
    """Export timestamps ("2024-03-25T01:59:07.356493Z" or "+00:00") → unix
    float, the schema's native time. Falls back rather than failing: a lost
    timestamp shouldn't lose the message."""
    if s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback if fallback is not None else db.now()


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _tool_marker(block: dict) -> str:
    """One compact line per tool call keeps the narrative ("wrote file X,
    then edited Y") and makes paths/titles searchable without dumping
    payloads into the transcript."""
    inp = block.get("input") or {}
    arg = next((inp[k] for k in ("path", "file_path", "title", "id", "query", "url")
                if inp.get(k)), "")
    arg = " ".join(str(arg).split())[:120]
    name = block.get("name") or "tool"
    return f"[tool: {name} {arg}]" if arg else f"[tool: {name}]"


def _artifact_attachment(block: dict) -> dict | None:
    """Artifacts are work product that exists nowhere else (unlike filesystem
    tool writes, which landed on the user's disk) — store each create/rewrite/
    update payload whole, as a searchable attachment."""
    inp = block.get("input") or {}
    parts = []
    if inp.get("title"):
        parts.append(f"# {inp['title']}")
    if inp.get("content"):
        parts.append(str(inp["content"]))
    if inp.get("old_str") or inp.get("new_str"):
        parts.append(f"[update]\n--- old\n{inp.get('old_str', '')}\n"
                     f"--- new\n{inp.get('new_str', '')}")
    text = "\n\n".join(parts).strip()
    if not text:
        return None
    return {"filename": f"{inp.get('id') or 'artifact'}.txt",
            "mime": "text/plain", "data_b64": _b64(text)}


def transform_message(m: dict) -> tuple[str, list[dict]]:
    """Export message → (content, attachments) for episodic.ingest. Text and
    thinking blocks carry the words; tool calls become one-line markers;
    tool_result blocks are dropped (bulky echoes of local state). Attachment
    extracted text and artifact payloads become attachment rows."""
    parts: list[str] = []
    attachments: list[dict] = []
    for b in m.get("content") or []:
        t = b.get("type")
        if t == "text" and (b.get("text") or "").strip():
            parts.append(b["text"].strip())
        elif t == "thinking" and (b.get("thinking") or "").strip():
            parts.append("[thinking]\n" + b["thinking"].strip())
        elif t == "tool_use":
            parts.append(_tool_marker(b))
            if b.get("name") == "artifacts":
                a = _artifact_attachment(b)
                if a:
                    attachments.append(a)
    if not parts and (m.get("text") or "").strip():
        parts.append(m["text"].strip())  # schema variants without blocks
    for f in m.get("files") or []:
        if f.get("file_name"):
            parts.append(f"[file: {f['file_name']}]")
    for a in m.get("attachments") or []:
        name = a.get("file_name") or "attachment"
        parts.append(f"[attachment: {name}]")
        text = (a.get("extracted_content") or "").strip()
        if text:
            attachments.append({"filename": name, "mime": "text/plain",
                                "data_b64": _b64(text)})
    return "\n\n".join(parts).strip(), attachments


def import_conversations(convs: list, con, settings, source_app: str) -> dict:
    stats = {"conversations": 0, "conversations_empty": 0, "messages": 0,
             "messages_empty": 0, "attached": 0}
    for c in convs:
        conv_epoch = iso_to_epoch(c.get("created_at"))
        msgs = []
        for m in c.get("chat_messages") or []:
            content, atts = transform_message(m)
            if not content and not atts:
                stats["messages_empty"] += 1
                continue
            msgs.append({"external_id": m["uuid"],
                         "speaker": SPEAKER.get(m.get("sender"),
                                                m.get("sender") or "unknown"),
                         "content": content,
                         "created_at": iso_to_epoch(m.get("created_at"),
                                                    fallback=conv_epoch),
                         "attachments": atts})
        if not msgs:
            stats["conversations_empty"] += 1
            continue
        r = episodic.ingest(con, source_app, c["uuid"], msgs,
                            title=c.get("name") or "", settings=settings)
        # ingest stamps "now"; the record should carry the export's date.
        con.execute("UPDATE conversations SET created_at=? WHERE id=?",
                    (conv_epoch, r["conversation_id"]))
        stats["conversations"] += 1
        stats["messages"] += r["ingested"]
        stats["attached"] += r["attached"]
    return stats


def import_projects(projects: list, con, settings, source_app: str) -> dict:
    stats = {"projects": 0, "docs": 0, "project_attached": 0}
    for p in projects:
        epoch = iso_to_epoch(p.get("created_at"))
        head = [f"[project] {p.get('name') or ''}".strip()]
        if p.get("description"):
            head.append(p["description"])
        if p.get("prompt_template"):
            head.append("[prompt template]\n" + p["prompt_template"])
        msgs = [{"external_id": f"project-{p['uuid']}", "speaker": "user",
                 "content": "\n\n".join(head).strip(),
                 "created_at": epoch, "attachments": []}]
        for d in p.get("docs") or []:
            text = (d.get("content") or "").strip()
            msgs.append({"external_id": d["uuid"], "speaker": "user",
                         "content": f"[project doc: {d.get('filename') or ''}]",
                         "created_at": iso_to_epoch(d.get("created_at"),
                                                    fallback=epoch),
                         "attachments": [{"filename": d.get("filename") or "doc.txt",
                                          "mime": "text/plain",
                                          "data_b64": _b64(text)}] if text else []})
            stats["docs"] += 1
        r = episodic.ingest(con, source_app, f"project-{p['uuid']}", msgs,
                            title=f"Project: {p.get('name') or ''}",
                            settings=settings)
        con.execute("UPDATE conversations SET created_at=? WHERE id=?",
                    (epoch, r["conversation_id"]))
        stats["projects"] += 1
        stats["project_attached"] += r["attached"]
    return stats


def import_memory_snapshot(memories: list, con, settings, source_app: str,
                           account_uuid: str) -> int:
    """The account's saved-memory blob is a point-in-time profile, parts of it
    stale by definition — it lands as a searchable system-speaker transcript,
    never as ledger facts."""
    texts = [v.strip() for m in memories or [] for v in m.values()
             if isinstance(v, str) and v.strip()]
    if not texts:
        return 0
    msgs = [{"external_id": f"memory-{i}", "speaker": "system", "content": t,
             "created_at": db.now(), "attachments": []}
            for i, t in enumerate(texts)]
    episodic.ingest(con, source_app, f"memory-snapshot-{account_uuid}", msgs,
                    title="claude.ai saved memory (export snapshot)",
                    settings=settings)
    return len(texts)


def run(export_dir: Path, settings, source_app: str) -> dict:
    conv_path = export_dir / "conversations.json"
    if not conv_path.exists():
        sys.exit(f"not a claude.ai export (no conversations.json): {export_dir}")

    users = []
    users_path = export_dir / "users.json"
    if users_path.exists():
        users = json.loads(users_path.read_text())
    account_uuid = (users[0].get("uuid") if users else "") or export_dir.name

    db.init(settings)
    con = db.connect(settings.db_path)
    try:
        marker = f"{MARKER_PREFIX}:{account_uuid}"
        if con.execute("SELECT value FROM settings WHERE key=?",
                       (marker,)).fetchone():
            sys.exit(f"target {settings.db_path} already holds this account's "
                     f"export (settings key '{marker}') — refusing to repeat")

        stats = import_conversations(json.loads(conv_path.read_text()),
                                     con, settings, source_app)

        projects = []
        for pp in sorted((export_dir / "projects").glob("*.json")):
            d = json.loads(pp.read_text())
            projects.extend(d if isinstance(d, list) else [d])
        stats |= import_projects(projects, con, settings, source_app)

        mem_path = export_dir / "memories.json"
        memories = json.loads(mem_path.read_text()) if mem_path.exists() else []
        stats["memory_snapshots"] = import_memory_snapshot(
            memories, con, settings, source_app, account_uuid)

        con.execute("INSERT INTO settings(key, value) VALUES(?,?)",
                    (marker, f"source={export_dir} "
                             f"conversations={stats['conversations']} "
                             f"messages={stats['messages']}"))
        con.commit()
    finally:
        con.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export-dir", required=True, type=Path,
                    help="unzipped claude.ai export directory")
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="target data dir (default: settings / MEMORY_DATA_DIR)")
    ap.add_argument("--source-app", default="claude.ai")
    args = ap.parse_args()

    export_dir = args.export_dir.resolve()
    if not export_dir.is_dir():
        sys.exit(f"export dir not found: {export_dir}")

    settings = load_settings()
    if args.data_dir:
        settings = settings.model_copy(update={"data_dir": args.data_dir.resolve()})
    elif _service_up():
        sys.exit("the live service looks up on port 8901 and no --data-dir "
                 "override was given — stop the service first (or point "
                 "--data-dir at a throwaway copy)")

    stats = run(export_dir, settings, args.source_app)
    print(f"imported into {settings.db_path}:")
    print(f"  conversations: {stats['conversations']} "
          f"({stats['conversations_empty']} empty skipped), "
          f"messages: {stats['messages']} "
          f"({stats['messages_empty']} content-free skipped)")
    print(f"  attachments stored: {stats['attached'] + stats['project_attached']}, "
          f"projects: {stats['projects']} ({stats['docs']} docs), "
          f"memory snapshot entries: {stats['memory_snapshots']}")
    print("post-import: verify /v1/health, then spot-check episodic search "
          "for something only the old account would know.")
    return 0


def _service_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", SERVICE_PORT), timeout=0.5):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
