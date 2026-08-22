"""Ledger state machine + keyword-path recall (embeddings unavailable in CI)."""

import pytest

from memory_service import ledger, recall


def test_add_rejects_trivial_content(con, settings):
    with pytest.raises(ValueError):
        ledger.add_fact(con, "hi", settings)


def test_parse_id_query_only_fires_on_the_hash_form():
    # `#` is what disambiguates: a bare number is a legitimate thing to
    # search the TEXT for, so reading it as an id would break content search.
    assert ledger.parse_id_query("#12") == [12]
    assert ledger.parse_id_query(" #12, 34  56 ") == [12, 34, 56]
    assert ledger.parse_id_query("1990") is None      # a year, not an id
    assert ledger.parse_id_query("#abc") is None
    assert ledger.parse_id_query("#") is None
    assert ledger.parse_id_query("") is None


def test_ledger_id_query_looks_up_ids_not_text(con, settings):
    # The ledger search matches content, so "1990" finds facts whose TEXT
    # says 1990 — not fact id 1990. `#` switches it to an id lookup, so a list
    # of ids handed over by a diagnostic can be acted on directly.
    decoy = ledger.add_fact(con, "Alex bought the house in 1990.", settings)
    a = ledger.add_fact(con, "Alex works at Initech.", settings)
    b = ledger.add_fact(con, "Alex commutes by bicycle.", settings)

    # plain text query still searches content — no regression
    assert [f["id"] for f in ledger.list_facts(con, query="1990")] == [decoy["id"]]

    # the id form ignores the text match entirely
    assert [f["id"] for f in ledger.list_facts(con, query=f"#{a['id']}")] == [a["id"]]

    # a list, mixed separators, unknown ids simply absent rather than an error
    got = {f["id"] for f in ledger.list_facts(con, query=f"#{a['id']}, {b['id']} 999999")}
    assert got == {a["id"], b["id"]}


def test_ledger_id_query_is_never_trimmed_by_the_page_size(con, settings):
    # An explicit id list asks for exactly those rows; trimming it to the page
    # size would read as "those ids don't exist" — a silent wrong answer.
    ids = [ledger.add_fact(con, f"Alex owns record number {i} outright.",
                           settings)["id"] for i in range(5)]
    q = "#" + ",".join(str(i) for i in ids)
    assert {f["id"] for f in ledger.list_facts(con, query=q, limit=2)} == set(ids)


def test_supersession_lifecycle(con, settings):
    a = ledger.add_fact(con, "Alex lives in Springfield.", settings)
    b = ledger.add_fact(con, "Alex lives in Shelbyville now.", settings)
    assert ledger.mark_superseded(con, a["id"], b["id"])
    assert not ledger.mark_superseded(con, a["id"], b["id"])  # only once
    old = ledger.get_fact(con, a["id"])
    assert old["invalidated_at"] and old["superseded_by"] == b["id"]
    valid = ledger.list_facts(con, status="valid")
    assert [f["id"] for f in valid] == [b["id"]]


def test_review_queue_and_dismiss(con, settings):
    q = ledger.add_fact(con, "Alex collects vintage synths.", settings,
                        origin_agent="mcp:claude-code")
    assert [f["id"] for f in ledger.review_queue(con)] == [q["id"]]
    ledger.dismiss(con, q["id"])
    assert ledger.review_queue(con) == []           # left the queue…
    f = ledger.get_fact(con, q["id"])
    assert f["quarantined_at"] is not None          # …but stays quarantined


def test_dismiss_all_clears_the_whole_queue_non_destructively(con, settings):
    # Bulk twin of dismiss — for clearing a backlog made stale by a
    # filtering fix in one action. Same semantics as one-at-a-time dismiss:
    # every affected fact stays quarantined and in the ledger, only leaves the
    # review queue, and each remains reversible via approve.
    q1 = ledger.add_fact(con, "Alex collects vintage synths.", settings,
                         origin_agent="mcp:claude-code")
    q2 = ledger.add_fact(con, "Alex is allergic to shellfish.", settings,
                         origin_agent="mcp:claude-code")
    valid = ledger.add_fact(con, "Alex works at Initech.", settings)  # untouched
    assert len(ledger.review_queue(con)) == 2

    n = ledger.dismiss_all(con)

    assert n == 2
    assert ledger.review_queue(con) == []
    for fid in (q1["id"], q2["id"]):
        f = ledger.get_fact(con, fid)
        assert f["quarantined_at"] is not None      # still quarantined…
        assert f["review_dismissed_at"] is not None  # …now dismissed
    assert ledger.get_fact(con, valid["id"])["quarantined_at"] is None

    # Idempotent / safe on an already-empty queue.
    assert ledger.dismiss_all(con) == 0

    # Reversible per-fact, exactly like a single dismiss.
    assert ledger.approve(con, q1["id"])
    assert [f["id"] for f in ledger.review_queue(con)] == []
    assert ledger.get_fact(con, q1["id"])["quarantined_at"] is None


def test_quarantine_many_pulls_accepted_facts_out_of_canon(con, settings):
    # A fact accepted as canon that later turns out to be malformed had
    # no non-destructive treatment — DELETE erases it, supersede asserts a
    # replacement that a garbage row doesn't have. quarantine_many is that
    # third option, and it's a batch because the real cases are batches.
    good = ledger.add_fact(con, "Alex works at Initech.", settings)
    bad1 = ledger.add_fact(con, "Alex is using the multi-tool daily.", settings)
    bad2 = ledger.add_fact(con, "Public release of the thing.", settings)
    assert recall.recall(con, settings, "multi-tool") != []   # canon beforehand

    out = ledger.quarantine_many(con, [bad1["id"], bad2["id"]], "malformed")

    assert out == {"quarantined": [bad1["id"], bad2["id"]], "skipped": []}
    for fid in (bad1["id"], bad2["id"]):
        f = ledger.get_fact(con, fid)
        assert f["quarantined_at"] is not None        # out of canon…
        assert f["quarantine_reason"] == "malformed"  # …and says why
    assert recall.recall(con, settings, "multi-tool") == []   # gone from recall
    assert {f["id"] for f in ledger.review_queue(con)} == {bad1["id"], bad2["id"]}
    assert ledger.get_fact(con, good["id"])["quarantined_at"] is None


def test_quarantine_many_skips_rather_than_fails_and_is_reversible(con, settings):
    a = ledger.add_fact(con, "Alex keeps bees in the backyard.", settings)
    assert ledger.quarantine_many(con, [a["id"]], "junk")["quarantined"] == [a["id"]]

    # Re-running is a no-op and an unknown id is skipped, not an error — so a
    # partially-applied batch can be retried wholesale.
    out = ledger.quarantine_many(con, [a["id"], 999_999], "junk")
    assert out == {"quarantined": [], "skipped": [a["id"], 999_999]}
    assert ledger.quarantine_many(con, [], "junk") == {"quarantined": [], "skipped": []}

    # Reversible, exactly like any other quarantine.
    assert ledger.approve(con, a["id"])
    assert ledger.get_fact(con, a["id"])["quarantined_at"] is None
    assert recall.recall(con, settings, "bees") != []


def test_approve_restores_to_recall(con, settings):
    q = ledger.add_fact(con, "Alex collects vintage synthesizers.", settings,
                        origin_agent="mcp:claude-code")
    assert recall.recall(con, settings, "vintage synthesizers") == []
    ledger.approve(con, q["id"])
    hits = recall.recall(con, settings, "vintage synthesizers")
    assert [f["id"] for f in hits] == [q["id"]]


def test_recall_keyword_scoring_and_superseded_exclusion(con, settings):
    a = ledger.add_fact(con, "Alex lives in Springfield.", settings,
                        event_date=1600000000.0)
    b = ledger.add_fact(con, "Alex lives in Shelbyville.", settings)
    ledger.mark_superseded(con, a["id"], b["id"])
    hits = recall.recall(con, settings, "lives Shelbyville")
    assert [f["id"] for f in hits] == [b["id"]]
    audit = recall.recall(con, settings, "lives Springfield Shelbyville",
                          include_superseded=True)
    assert {f["id"] for f in audit} == {a["id"], b["id"]}


def test_recall_collapses_exact_duplicates(con, settings):
    ledger.add_fact(con, "Alex brews espresso every morning.", settings)
    ledger.add_fact(con, "Alex brews espresso every morning!", settings)  # same hash
    hits = recall.recall(con, settings, "espresso morning")
    assert len(hits) == 1


def test_empty_query_returns_recent_valid(con, settings):
    for i in range(3):
        ledger.add_fact(con, f"Alex fact number {i} about hobbies.", settings)
    hits = recall.recall(con, settings, "", limit=2)
    assert len(hits) == 2 and hits[0]["id"] > hits[1]["id"]


def test_update_fact_clears_embedding_and_rehashes(con, settings):
    a = ledger.add_fact(con, "Alex plays the violin badly.", settings)
    before = ledger.get_fact(con, a["id"])["content_hash"]
    ledger.update_fact(con, a["id"], content="Alex plays the violin beautifully.")
    after = ledger.get_fact(con, a["id"])
    assert after["content_hash"] != before


def test_importance_is_owner_correctable(con, settings):
    """The human outranks the miner: importance decides how a fact ages, so
    the PATCH surface must allow correcting it (clamped 1-10)."""
    from memory_service import ledger
    f = ledger.add_fact(con, "Alex's wife is named Sam.", settings, importance=7)
    assert ledger.update_fact(con, f["id"], importance=9)
    assert ledger.get_fact(con, f["id"])["importance"] == 9
    ledger.update_fact(con, f["id"], importance=99)  # clamped, not rejected
    assert ledger.get_fact(con, f["id"])["importance"] == 10


def test_facts_embed_at_write_time(con, settings, monkeypatch):
    """The recall after a save must not pay the provider round-trip: writes
    spawn a background embed; the recall-path ensure stays as the
    keyless/failure safety net."""
    import time
    from memory_service import embeddings, ledger
    monkeypatch.setattr(embeddings, "available", lambda settings=None: True)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, settings: [[0.5] * 8 for _ in texts])
    f = ledger.add_fact(con, "Alex plays chess on Sundays.", settings)
    for _ in range(50):  # background thread: poll briefly
        row = con.execute("SELECT embedding FROM facts WHERE id=?", (f["id"],)).fetchone()
        if row["embedding"] is not None:
            break
        time.sleep(0.02)
    assert row["embedding"] is not None
    assert embeddings.unpack(row["embedding"])[0] == 0.5


def test_write_embed_failure_defers_to_recall_path(con, settings, monkeypatch):
    from memory_service import embeddings, ledger
    monkeypatch.setattr(embeddings, "available", lambda settings=None: True)
    def boom(texts, settings):
        raise RuntimeError("provider down")
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    f = ledger.add_fact(con, "Alex dislikes early mornings.", settings)  # must not raise
    import time; time.sleep(0.1)
    row = con.execute("SELECT embedding FROM facts WHERE id=?", (f["id"],)).fetchone()
    assert row["embedding"] is None  # deferred — ensure_fact_embeddings covers it


def test_keyless_write_spawns_nothing(con, settings):
    from memory_service import ledger
    import threading
    before = threading.active_count()
    ledger.add_fact(con, "Alex enjoys hiking trails.", settings)
    assert threading.active_count() == before  # no key: no thread, no error
