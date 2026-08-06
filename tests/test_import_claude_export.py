"""The claude.ai export importer: transforms, timestamps, idempotence guard.

Fixture data is synthetic — the shape mirrors a real claude.ai "Export data"
directory (conversations.json / projects/*.json / memories.json / users.json)
without containing anyone's actual history.
"""

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from memory_service import db as db_mod
from memory_service import episodic

_SPEC = importlib.util.spec_from_file_location(
    "import_claude_export",
    Path(__file__).resolve().parent.parent / "scripts" / "import_claude_export.py")
imp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(imp)

CONV_ISO = "2024-03-25T01:59:07.356493Z"


def make_export(tmp_path: Path) -> Path:
    d = tmp_path / "export"
    (d / "projects").mkdir(parents=True)
    (d / "users.json").write_text(json.dumps([
        {"uuid": "acct-0000", "full_name": "Alex Example",
         "email_address": "you@example.com"}]))
    (d / "conversations.json").write_text(json.dumps([
        {"uuid": "conv-1", "name": "Trip planning", "created_at": CONV_ISO,
         "updated_at": CONV_ISO,
         "chat_messages": [
             {"uuid": "m1", "sender": "human",
              "created_at": "2024-03-25T02:00:00.000000Z",
              "text": "Planning a trip to Hobart",
              "content": [{"type": "text", "text": "Planning a trip to Hobart"}],
              "attachments": [{"file_name": "itinerary.pdf", "file_size": 21,
                               "file_type": "application/pdf",
                               "extracted_content": "Day one: museum visit"}],
              "files": [{"file_uuid": "f1", "file_name": "photo.png"}]},
             {"uuid": "m2", "sender": "assistant",
              "created_at": "2024-03-25T02:00:05.000000Z",
              "text": "Sounds fun",
              "content": [
                  {"type": "thinking", "thinking": "consider ferries"},
                  {"type": "text", "text": "Sounds fun"},
                  {"type": "tool_use", "name": "artifacts",
                   "input": {"id": "plan-doc", "title": "Plan",
                             "command": "create",
                             "content": "# Itinerary\nferry timetable"}},
                  {"type": "tool_result", "name": "artifacts",
                   "content": [{"type": "text", "text": "OK"}]}],
              "attachments": [], "files": []},
             {"uuid": "m3", "sender": "human",
              "created_at": "2024-03-25T02:00:10.000000Z", "text": "",
              "content": [], "attachments": [], "files": []},
         ]},
        {"uuid": "conv-empty", "name": "Empty", "created_at": CONV_ISO,
         "updated_at": CONV_ISO, "chat_messages": []},
    ]))
    (d / "projects" / "p1.json").write_text(json.dumps(
        {"uuid": "p1", "name": "Garden", "description": "Backyard planning",
         "prompt_template": "Be practical.",
         "created_at": "2025-02-15T11:41:22.303456+00:00",
         "docs": [{"uuid": "doc-1", "filename": "beds.md",
                   "content": "Raised beds layout",
                   "created_at": "2025-02-15T11:41:22.303456+00:00"}]}))
    (d / "memories.json").write_text(json.dumps(
        [{"conversations_memory": "Alex enjoys gardening."}]))
    return d


@pytest.fixture
def imported(settings, tmp_path):
    export = make_export(tmp_path)
    stats = imp.run(export, settings, "claude.ai")
    con = db_mod.connect(settings.db_path)
    yield stats, con, export
    con.close()


def test_counts(imported):
    stats, _, _ = imported
    assert stats["conversations"] == 1
    assert stats["conversations_empty"] == 1
    assert stats["messages"] == 2          # m3 is content-free and skipped
    assert stats["messages_empty"] == 1
    assert stats["attached"] == 2          # itinerary text + artifact payload
    assert stats["projects"] == 1
    assert stats["docs"] == 1
    assert stats["project_attached"] == 1
    assert stats["memory_snapshots"] == 1


def test_conversation_timestamps_and_speakers(imported):
    _, con, _ = imported
    conv = episodic.get_conversation(con, "claude.ai", "conv-1")
    want = datetime.fromisoformat(CONV_ISO.replace("Z", "+00:00")).timestamp()
    assert abs(conv["created_at"] - want) < 1   # export date, not import date
    msgs = episodic.messages_after(con, conv["id"])
    assert [m["speaker"] for m in msgs] == ["user", "claude"]
    assert msgs[0]["created_at"] > want


def test_content_transforms(imported):
    _, con, _ = imported
    conv = episodic.get_conversation(con, "claude.ai", "conv-1")
    human, assistant = episodic.messages_after(con, conv["id"])
    assert "[file: photo.png]" in human["content"]
    assert "[attachment: itinerary.pdf]" in human["content"]
    assert "[thinking]\nconsider ferries" in assistant["content"]
    assert "[tool: artifacts Plan]" in assistant["content"]
    assert "OK" not in assistant["content"]     # tool_result dropped


def test_attachments_searchable(imported):
    _, con, _ = imported
    hits = episodic.search(con, "museum")
    assert any("museum" in h["content"] for h in hits)
    assert any("timetable" in h["content"]
               for h in episodic.search(con, "timetable"))  # artifact payload
    assert any("Raised" in h["content"] for h in episodic.search(con, "beds"))


def test_project_and_memory_snapshot(imported):
    _, con, _ = imported
    proj = episodic.get_conversation(con, "claude.ai", "project-p1")
    assert proj["title"] == "Project: Garden"
    snap = episodic.get_conversation(con, "claude.ai", "memory-snapshot-acct-0000")
    msgs = episodic.messages_after(con, snap["id"])
    assert [m["speaker"] for m in msgs] == ["system"]
    assert "gardening" in msgs[0]["content"]
    assert episodic.get_conversation(con, "claude.ai", "conv-empty") is None


def test_no_ledger_writes(imported):
    _, con, _ = imported
    assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_rerun_refused(imported, settings):
    _, _, export = imported
    with pytest.raises(SystemExit, match="refusing to repeat"):
        imp.run(export, settings, "claude.ai")
