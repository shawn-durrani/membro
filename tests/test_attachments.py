"""Attachments in the episodic record — never lose this stuff.

Files that travel with messages are stored whole (content-addressed bytes on
disk), their text extracted where possible, searchable via /v1/search, and
mined for facts alongside the message they arrived with. Append-only and
idempotent like everything else in the record.
"""

import base64

from fastapi.testclient import TestClient

from memory_service import episodic
from memory_service.api import create_app

DOC = ("Alex signed the workshop lease at 42 Foundry Lane on 2026-05-01. "
       "The landlord is Harbor Holdings.")


def _msg(att=None, ext_id="m1"):
    m = {"external_id": ext_id, "speaker": "user", "content": "see attached",
         "created_at": 1700000000.0}
    if att:
        m["attachments"] = att
    return m


def _txt(name="lease.txt", text=DOC):
    return {"filename": name, "mime": "text/plain",
            "data_b64": base64.b64encode(text.encode()).decode()}


def test_ingest_stores_bytes_and_text(con, settings):
    res = episodic.ingest(con, "app", "c1", [_msg([_txt()])], settings=settings)
    assert res["attached"] == 1
    row = con.execute("SELECT * FROM attachments").fetchone()
    assert row["filename"] == "lease.txt" and row["size"] == len(DOC.encode())
    path = settings.data_dir / "attachments" / row["stored_name"]
    assert path.read_bytes().decode() == DOC  # bytes stored whole
    assert "Foundry Lane" in row["extracted_text"]


def test_ingest_attachments_idempotent_and_backfillable(con, settings):
    episodic.ingest(con, "app", "c1", [_msg()], settings=settings)  # no attachment
    # same message re-ingested WITH the attachment: message skipped, file lands
    res = episodic.ingest(con, "app", "c1", [_msg([_txt()])], settings=settings)
    assert res["skipped"] == 1 and res["attached"] == 1
    # third pass: pure no-op
    res = episodic.ingest(con, "app", "c1", [_msg([_txt()])], settings=settings)
    assert res["attached"] == 0
    assert con.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1


def test_attachment_text_is_searchable(con, settings):
    episodic.ingest(con, "app", "c1", [_msg([_txt()])], title="Lease chat",
                    settings=settings)
    hits = episodic.search(con, "Foundry lease")
    assert hits and any(h["speaker"] == "file: lease.txt" for h in hits)
    assert any("Foundry" in h["content"] for h in hits)


def test_attachment_facts_are_minable(con, settings, fake_llm):
    """A pasted document's facts are as real as typed ones — the miner sees
    the attachment text and the grounding wall grounds against it."""
    from memory_service import mining
    app = settings.trusted_apps[0]  # trusted source: the write gate stands down
    episodic.ingest(con, app, "c1", [_msg([_txt()])], settings=settings)
    fake_llm["response"] = (
        "NEW importance=6: Alex leases a workshop at 42 Foundry Lane from "
        "Harbor Holdings.")
    res = mining.distill(con, settings, app, "c1", regenerate=False)
    assert res["added"] == 1
    assert "Foundry Lane" in fake_llm["prompts"][0]  # miner saw the document
    fact = con.execute("SELECT * FROM facts").fetchone()
    # "Harbor Holdings" appears only in the attachment — grounded, no quarantine
    assert fact["quarantined_at"] is None


def test_binary_stored_without_text(con, settings):
    png = {"filename": "photo.png", "mime": "image/png",
           "data_b64": base64.b64encode(b"\x89PNG fakebytes").decode()}
    res = episodic.ingest(con, "app", "c1", [_msg([png])], settings=settings)
    assert res["attached"] == 1
    row = con.execute("SELECT * FROM attachments").fetchone()
    assert row["extracted_text"] == ""  # nothing searchable, bytes kept whole
    path = settings.data_dir / "attachments" / row["stored_name"]
    assert path.read_bytes() == b"\x89PNG fakebytes"


def test_pdf_text_extraction(con, settings):
    from pypdf import PdfWriter
    import io
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    pdf = {"filename": "doc.pdf", "mime": "application/pdf",
           "data_b64": base64.b64encode(buf.getvalue()).decode()}
    res = episodic.ingest(con, "app", "c1", [_msg([pdf])], settings=settings)
    assert res["attached"] == 1  # stored; blank page → empty text, no crash


def test_ingest_endpoint_carries_attachments(settings):
    app = create_app(settings)
    auth = {"Authorization": f"Bearer {app.state.admin_token}"}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        r = client.post("/v1/ingest", json={
            "source_app": "app", "conversation_id": "c9",
            "messages": [{"external_id": "m1", "speaker": "user",
                          "content": "attached", "created_at": "2026-07-11T09:00:00",
                          "attachments": [_txt()]}]})
        assert r.json() == {"ingested": 1, "skipped": 0, "attached": 1}
        hits = client.post("/v1/search", json={"query": "Foundry"}).json()["hits"]
        assert any(h["speaker"].startswith("file:") for h in hits)


def test_no_automated_delete_or_update_of_attachments():
    """Append-only with ONE exception, mirroring facts: the human-initiated
    eraser in api.py (danger zone). No other module may delete or update."""
    import pathlib

    from memory_service import episodic as mod
    for p in pathlib.Path(mod.__file__).parent.glob("*.py"):
        src = p.read_text().lower()
        if p.name != "api.py":
            assert "delete from attachments" not in src, p.name
        assert "update attachments" not in src, p.name


def test_browse_download_and_human_delete(settings):
    """The Files surface: list with conversation context, download the exact
    bytes, and the danger-zone delete — row, FTS entry, and (unshared) bytes."""
    app = create_app(settings)
    # the attachment routes are owner-gated: they expose exact fact
    # rows, message bodies and document text, so they need a credential
    # even on loopback — same rule as /v1/facts and /v1/review.
    auth = {"Authorization": f"Bearer {app.state.admin_token}"}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        client.post("/v1/ingest", json={
            "source_app": "app", "conversation_id": "c1", "title": "Lease chat",
            "messages": [{"external_id": "m1", "speaker": "user",
                          "content": "attached", "created_at": "2026-07-11T09:00:00",
                          "attachments": [_txt()]}]})
        # browse
        atts = client.get("/v1/attachments").json()["attachments"]
        assert len(atts) == 1
        a = atts[0]
        assert a["filename"] == "lease.txt" and a["conversation_title"] == "Lease chat"
        assert a["searchable"] == 1
        # download: the exact original bytes
        r = client.get(f"/v1/attachments/{a['id']}/file")
        assert r.status_code == 200 and r.content.decode() == DOC
        # delete: row gone, unsearchable, bytes unlinked
        res = client.delete(f"/v1/attachments/{a['id']}").json()
        assert res == {"deleted": a["id"], "file_removed": True}
        assert client.get("/v1/attachments").json()["attachments"] == []
        assert client.get(f"/v1/attachments/{a['id']}/file").status_code == 404
        hits = client.post("/v1/search", json={"query": "Foundry"}).json()["hits"]
        assert not any(h["speaker"].startswith("file:") for h in hits)


def test_preview_carries_content_and_provenance(settings, fake_llm):
    """Preview = the file in context: text excerpt, the message it arrived
    with, and the ledger facts mined from that conversation — no bare ids."""
    from memory_service import ledger
    app = create_app(settings)
    # the attachment routes are owner-gated: they expose exact fact
    # rows, message bodies and document text, so they need a credential
    # even on loopback — same rule as /v1/facts and /v1/review.
    auth = {"Authorization": f"Bearer {app.state.admin_token}"}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        client.post("/v1/ingest", json={
            "source_app": "app", "conversation_id": "c1", "title": "Lease chat",
            "messages": [{"external_id": "m1", "speaker": "user",
                          "content": "here is the lease doc",
                          "created_at": "2026-07-11T09:00:00",
                          "attachments": [_txt()]}]})
        # a fact mined from that conversation (conversation rowid is 1)
        import sqlite3
        con = sqlite3.connect(settings.db_path)
        con.row_factory = sqlite3.Row
        ledger.add_fact(con, "Alex leases a workshop at Foundry Lane.", settings,
                        conversation_id=1, importance=6)
        con.close()

        att_id = client.get("/v1/attachments").json()["attachments"][0]["id"]
        p = client.get(f"/v1/attachments/{att_id}/preview").json()
        assert p["kind"] == "text" and "Foundry Lane" in p["text"]
        assert p["message"]["content"] == "here is the lease doc"
        assert p["conversation_title"] == "Lease chat"
        assert any("workshop" in f["content"] for f in p["facts"])
        # inline serves for in-browser preview; default downloads
        inline = client.get(f"/v1/attachments/{att_id}/file?inline=1")
        assert "attachment" not in inline.headers.get("content-disposition", "")
        dl = client.get(f"/v1/attachments/{att_id}/file")
        assert "attachment" in dl.headers.get("content-disposition", "")
        assert client.get("/v1/attachments/999/preview").status_code == 404


def test_delete_keeps_shared_bytes(settings):
    """Bytes are content-addressed: deleting one row must not take the file
    out from under another message carrying the same document."""
    app = create_app(settings)
    # the attachment routes are owner-gated: they expose exact fact
    # rows, message bodies and document text, so they need a credential
    # even on loopback — same rule as /v1/facts and /v1/review.
    auth = {"Authorization": f"Bearer {app.state.admin_token}"}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        for ext in ("m1", "m2"):
            client.post("/v1/ingest", json={
                "source_app": "app", "conversation_id": "c1",
                "messages": [{"external_id": ext, "speaker": "user",
                              "content": "attached", "created_at": "2026-07-11T09:00:00",
                              "attachments": [_txt()]}]})
        atts = client.get("/v1/attachments").json()["attachments"]
        assert len(atts) == 2
        res = client.delete(f"/v1/attachments/{atts[0]['id']}").json()
        assert res["file_removed"] is False  # the other row still needs it
        r = client.get(f"/v1/attachments/{atts[1]['id']}/file")
        assert r.status_code == 200 and r.content.decode() == DOC


def test_ingest_caps_attachments_per_message(settings):
    """Per-file size is bounded; the per-message COUNT must be too, or one
    request balloons past what the file bound intended (2026-07-11 pass)."""
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        r = client.post("/v1/ingest", json={
            "source_app": "app", "conversation_id": "c1",
            "messages": [{"external_id": "m1", "speaker": "user",
                          "content": "x", "created_at": "2026-07-11T09:00:00",
                          "attachments": [_txt(f"f{i}.txt") for i in range(21)]}]})
        assert r.status_code == 422
