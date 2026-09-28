"""A search hit shows enough of its message to carry the detail (#150).

The benchmark case: the right message came back first, but its 24-word
excerpt stopped before the number the search was for."""

from memory_service import episodic

FILLER = " ".join(f"word{i}" for i in range(30))
TEXT = (f"I packed a lot of shoes for my last trip. {FILLER} "
        "In the end I was only wearing two pairs.")


def test_hit_reaches_a_detail_thirty_words_past_the_match(con):
    episodic.ingest(con, "multi-model-chat", "chat-s", [
        {"external_id": "s1", "speaker": "user", "content": TEXT,
         "created_at": 1700000000.0}])
    hits = episodic.search(con, "packed shoes")
    assert len(hits) == 1
    assert "only wearing two" in hits[0]["content"]


def test_hit_is_still_an_excerpt(con):
    long = " ".join(f"word{i}" for i in range(200)) + " shoes"
    episodic.ingest(con, "multi-model-chat", "chat-s", [
        {"external_id": "s1", "speaker": "user", "content": long,
         "created_at": 1700000000.0}])
    words = episodic.search(con, "shoes")[0]["content"].split()
    assert 60 <= len(words) <= 66    # 64 tokens, plus the ellipsis


def test_attachment_hits_get_the_same_excerpt(con):
    episodic.ingest(con, "multi-model-chat", "chat-a", [
        {"external_id": "a1", "speaker": "user", "content": "see attached",
         "created_at": 1700000000.0}])
    cid = episodic.get_conversation(con, "multi-model-chat", "chat-a")["id"]
    con.execute(
        "INSERT INTO attachments(conversation_id, message_external_id, filename,"
        " mime, size, sha256, stored_name, extracted_text, created_at) "
        "VALUES(?, 'a1', 'notes.txt', 'text/plain', 1, 'x', 'x', ?, ?)",
        (cid, TEXT, 1700000000.0))
    con.commit()
    hits = [h for h in episodic.search(con, "packed shoes")
            if h["speaker"].startswith("file:")]
    assert hits and "only wearing two" in hits[0]["content"]
