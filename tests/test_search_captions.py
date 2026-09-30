"""A search hit on a photo's caption shows the caption around the match (#160).

A captioned image is indexed by its caption, but its own text is empty, so
the hit used to come back with a blank excerpt: the right photo, and only
its file name. All keyless: the vision call is a lambda."""

from memory_service import captions, episodic, erasers

PNG = b"\x89PNG\r\n\x1a\n" + b"fakepixels" * 20
FILLER = " ".join(f"word{i}" for i in range(30))
HIT_KEYS = {"speaker", "created_at", "title", "conversation_id", "content",
            "web_sources"}


def _photos(con, settings, *texts):
    """One chat, one message, and a captioned photo for each text, in order.
    Returns the attachment ids."""
    episodic.ingest(con, "multi-model-chat", "chat-img", [
        {"external_id": "m1", "speaker": "user", "content": "photos attached",
         "created_at": 1700000000.0}], title="Build photos")
    conv = episodic.get_conversation(con, "multi-model-chat", "chat-img")
    for i, _ in enumerate(texts):
        episodic.add_attachment(con, settings, conv["id"], "m1",
                                f"photo{i}.png", "image/png", PNG + bytes([i]))
    replies = iter(texts)
    captions.caption_pending(con, settings, conv["id"],
                             vision=lambda *a, **k: next(replies))
    return [r[0] for r in con.execute("SELECT id FROM attachments ORDER BY id")]


def _file_hits(con, query):
    return [h for h in episodic.search(con, query)
            if h["speaker"].startswith("file:")]


def test_caption_hit_carries_the_caption_around_the_match(con, settings):
    _photos(con, settings,
            f"A shelf of packed boxes. {FILLER} A label on the top box reads 42 kg.")
    hits = _file_hits(con, "boxes")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["speaker"] == "file: photo0.png (image caption)"
    assert ">>boxes<<" in hit["content"]
    assert "reads 42 kg" in hit["content"]      # 30 words past the match
    assert set(hit) == HIT_KEYS                 # no helper columns on the wire


def test_a_long_caption_is_cut_to_an_excerpt(con, settings):
    long = (" ".join(f"word{i}" for i in range(80)) + " two ladders "
            + " ".join(f"tail{i}" for i in range(80)))
    _photos(con, settings, long)
    content = _file_hits(con, "ladders")[0]["content"]
    assert ">>ladders<<" in content
    assert content.startswith(" … ") and content.endswith(" … ")
    assert 60 <= len(content.split()) <= 66     # 64 words, plus the ellipses


def test_caption_hit_marks_a_different_word_form(con, settings):
    _photos(con, settings, "Boxes packed into the back of a van.")
    assert ">>packed<<" in _file_hits(con, "packing")[0]["content"]


def test_a_short_caption_comes_back_whole_with_its_marker(con, settings):
    _photos(con, settings, "Three softwood boards beside a track saw.")
    content = _file_hits(con, "boards")[0]["content"]
    assert content.startswith("[" + captions.CAPTION_MARKER)
    assert "Three softwood >>boards<< beside a track saw.]" in content


def test_a_document_hit_keeps_its_plain_label(con, settings):
    _photos(con, settings, "Two ladders against a wall.")
    conv = episodic.get_conversation(con, "multi-model-chat", "chat-img")
    episodic.add_attachment(con, settings, conv["id"], "m1", "notes.txt",
                            "text/plain", b"Borrow the ladders on Friday.")
    hits = {h["speaker"]: h for h in _file_hits(con, "ladders")}
    assert set(hits) == {"file: notes.txt", "file: photo0.png (image caption)"}
    assert ">>ladders<<" in hits["file: notes.txt"]["content"]
    assert all(set(h) == HIT_KEYS for h in hits.values())


def test_an_erased_photo_caption_never_comes_back_in_search(con, settings):
    """Erasing a photo takes its caption's words out of the index and its
    caption row out of the table. Together that means a search can neither
    match the erased photo nor cut an excerpt from its caption, while a
    photo that's still there keeps its own."""
    gone, kept = _photos(con, settings,
                         "Three marblewood planks stacked by a window.",
                         "Two marblewood offcuts on a bench.")
    before = {h["speaker"]: h["content"] for h in _file_hits(con, "marblewood")}
    assert "planks" in before["file: photo0.png (image caption)"]
    assert "offcuts" in before["file: photo1.png (image caption)"]

    assert erasers.erase_attachment(con, settings, gone)["deleted"] == gone

    hits = episodic.search(con, "marblewood planks window")
    assert [h["speaker"] for h in hits] == ["file: photo1.png (image caption)"]
    assert "offcuts" in hits[0]["content"]
    assert not any(w in h["content"] for h in hits for w in ("planks", "window"))
    assert episodic.search(con, "window") == []
    assert [r[0] for r in con.execute(
        "SELECT attachment_id FROM attachment_captions")] == [kept]
    con.execute("CREATE VIRTUAL TABLE temp.att_terms "
                "USING fts5vocab(main, attachments_fts, 'instance')")
    assert con.execute("SELECT COUNT(*) FROM temp.att_terms WHERE doc = ?",
                       (gone,)).fetchone()[0] == 0


def test_a_caption_the_match_misses_shows_its_opening():
    """The index and the caption can't disagree today, but if they ever did
    the hit would still show the caption's opening words, not a blank."""
    hit = {"speaker": "file: a.png", "content": "", "attachment_id": 7,
           "caption": " ".join(f"word{i}" for i in range(100))}
    [out] = episodic._with_caption_excerpts([hit], '"elsewhere"')
    assert out["content"].startswith("word0 ") and out["content"].endswith(" … ")
    assert out["speaker"] == "file: a.png (image caption)"
    assert "caption" not in out and "attachment_id" not in out
