"""Image captions at distill time (#107): photos stop being invisible to memory.

All keyless: the vision call is injected or monkeypatched, never real.
"""

import pytest

from memory_service import captions, episodic, llm, mining

PNG = b"\x89PNG\r\n\x1a\n" + b"fakepixels" * 20


def _ingest_photo_chat(con, settings, mime="image/png", data=PNG,
                       filename="shelf.png"):
    msgs = [{"external_id": "m1", "speaker": "user",
             "content": "Here's where the bookshelf build is at.",
             "created_at": 1700000000.0,
             "attachments": []}]
    episodic.ingest(con, "multi-model-chat", "chat-img", msgs, title="Build photos")
    conv = episodic.get_conversation(con, "multi-model-chat", "chat-img")
    episodic.add_attachment(con, settings, conv["id"], "m1", filename, mime, data)
    return conv


def test_images_store_with_empty_text_and_get_captioned(con, settings):
    conv = _ingest_photo_chat(con, settings)
    assert captions.pending_images(con, conv["id"]), "image should start uncaptioned"

    calls = []

    def fake_vision(prompt, images, settings_, max_tokens=400, model=None):
        calls.append((prompt, images))
        return "Four planed softwood boards, approx 30mm thick, on a workbench."

    result = captions.caption_pending(con, settings, conv["id"], vision=fake_vision)
    assert result == {"captioned": 1, "skipped": 0}
    assert len(calls) == 1
    # the image bytes actually went to the model
    assert calls[0][1][0][0] == "image/png" and calls[0][1][0][1]

    # caption lives in its OWN table; the attachments row is untouched
    assert con.execute("SELECT extracted_text FROM attachments").fetchone()[0] == ""
    row = con.execute("SELECT caption FROM attachment_captions").fetchone()
    assert "softwood boards" in row["caption"]


def test_caption_is_marked_machine_generated_with_model_name(con, settings):
    conv = _ingest_photo_chat(con, settings)
    captions.caption_pending(con, settings, conv["id"],
                             vision=lambda *a, **k: "A grey cat on a chair.")
    row = con.execute(
        "SELECT caption, model FROM attachment_captions").fetchone()
    assert captions.CAPTION_MARKER in row["caption"]
    assert settings.miner_model in row["caption"] and row["model"] == settings.miner_model


def test_caption_runs_once_then_never_again(con, settings):
    conv = _ingest_photo_chat(con, settings)
    counter = {"n": 0}

    def fake_vision(*a, **k):
        counter["n"] += 1
        return "One board."

    captions.caption_pending(con, settings, conv["id"], vision=fake_vision)
    captions.caption_pending(con, settings, conv["id"], vision=fake_vision)
    assert counter["n"] == 1, "a stored caption must be reused, not re-bought"


@pytest.mark.parametrize("mime,data", [
    ("image/heic", PNG),                        # unsupported format
    ("image/png", b"x" * (captions.MAX_IMAGE_BYTES + 1)),  # oversized
])
def test_unsupported_or_oversized_images_are_skipped_not_guessed(
        con, settings, mime, data):
    conv = _ingest_photo_chat(con, settings, mime=mime, data=data)
    result = captions.caption_pending(
        con, settings, conv["id"],
        vision=lambda *a, **k: pytest.fail("vision must not be called"))
    assert result["captioned"] == 0 and result["skipped"] == 1
    assert con.execute("SELECT COUNT(*) FROM attachment_captions").fetchone()[0] == 0


def test_keyless_skips_and_never_blocks(con, settings):
    conv = _ingest_photo_chat(con, settings)

    def no_key(*a, **k):
        raise llm.MissingKeyError("no key")

    result = captions.caption_pending(con, settings, conv["id"], vision=no_key)
    assert result["captioned"] == 0
    # still pending: retried on a future distill once a key exists
    assert captions.pending_images(con, conv["id"])


def test_one_bad_image_does_not_stop_the_rest(con, settings):
    conv = _ingest_photo_chat(con, settings)
    episodic.add_attachment(con, settings, conv["id"], "m1", "second.png",
                            "image/png", PNG + b"different")
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("api hiccup")
        return "A single pine offcut."

    result = captions.caption_pending(con, settings, conv["id"], vision=flaky)
    assert result == {"captioned": 1, "skipped": 1}


def test_distill_mines_facts_from_the_caption(con, settings, fake_llm, monkeypatch):
    """The end-to-end point of #107: photo content becomes ledger content."""
    conv = _ingest_photo_chat(con, settings)
    monkeypatch.setattr(
        "memory_service.captions.llm.utility_vision",
        lambda *a, **k: "Four planed 30mm softwood boards on a workbench.")
    fake_llm["response"] = ("NEW importance=5: Alex is dimensioning 30mm "
                            "softwood boards for a bookshelf build.")

    mining.distill(con, settings, "multi-model-chat", "chat-img",
                   regenerate=False)

    # the caption reached the miner's view of the message...
    assert any("30mm softwood boards" in p for p in fake_llm["prompts"])
    # ...and the mined fact landed
    facts = [r["content"] for r in con.execute("SELECT content FROM facts")]
    assert any("30mm" in f for f in facts)


def test_caption_model_setting_overrides_the_miner(con, settings):
    """caption_model exists for text-only miners; the marker and provenance
    column must name the model that actually ran."""
    settings.caption_model = "gpt-5-mini"
    conv = _ingest_photo_chat(con, settings)
    seen = {}

    def fake_vision(prompt, images, settings_, max_tokens=400, model=None):
        seen["model"] = model
        return "A board."

    captions.caption_pending(con, settings, conv["id"], vision=fake_vision)
    assert seen["model"] == "gpt-5-mini"
    row = con.execute("SELECT caption, model FROM attachment_captions").fetchone()
    assert row["model"] == "gpt-5-mini" and "gpt-5-mini" in row["caption"]


def test_caption_prompt_carries_no_animal_or_species_examples():
    """Example phrases leak into outputs; the prompt must not seed the exact
    attribute classes it forbids (species was the motivating failure)."""
    for banned in ("cat", "dog", "tabby", "Fir", "pine"):
        assert banned not in captions._PROMPT, banned


def test_caption_prompt_forbids_species_and_brand_guessing():
    """The grounding rules are the point of #107 — pin them."""
    for required in ("literally visible", "Do NOT identify", "species",
                     "generic terms"):
        assert required in captions._PROMPT


def test_caption_is_searchable_and_attachment_row_untouched(con, settings):
    """The FTS overlay: /v1/search sees what the photo showed, while the
    attachments row keeps its empty extracted_text forever."""
    conv = _ingest_photo_chat(con, settings)
    captions.caption_pending(
        con, settings, conv["id"],
        vision=lambda *a, **k: "Three softwood boards beside a track saw.")
    hits = [dict(r) for r in con.execute(
        "SELECT a.filename FROM attachments_fts f JOIN attachments a "
        "ON a.id = f.rowid WHERE attachments_fts MATCH 'softwood'")]
    assert hits and hits[0]["filename"] == "shelf.png"
    assert con.execute("SELECT extracted_text FROM attachments").fetchone()[0] == ""


def test_captions_table_is_append_only_outside_api():
    """Extends the attachments invariant to the captions table: no module but
    api.py (the human-initiated eraser) may update or delete captions."""
    import pathlib

    from memory_service import captions as mod
    for p in pathlib.Path(mod.__file__).parent.glob("*.py"):
        src = p.read_text().lower()
        if p.name != "api.py":
            assert "delete from attachment_captions" not in src, p.name
        assert "update attachment_captions" not in src, p.name


def test_human_attachment_delete_cascades_the_caption(con, settings):
    """The one sanctioned delete path removes the caption with the attachment
    (FK cascade), so no orphan machine text survives an eraser action."""
    conv = _ingest_photo_chat(con, settings)
    captions.caption_pending(con, settings, conv["id"],
                             vision=lambda *a, **k: "A board.")
    assert con.execute("SELECT COUNT(*) FROM attachment_captions").fetchone()[0] == 1
    con.execute("DELETE FROM attachments")  # what api.py's eraser does
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM attachment_captions").fetchone()[0] == 0
