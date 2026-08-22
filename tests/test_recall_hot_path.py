"""Recall hot-path mechanics: same answers, less work per call.

The refactor changed WHERE the math runs (one client, one query
normalization, one unpack per fact) and must not change a single ranking
or dedup decision. These tests pin the equivalences.
"""

import math

import pytest

from memory_service import db as db_mod
from memory_service import embeddings, ledger, recall


def test_unit_and_cosine_unit_match_cosine():
    a = [0.3, -1.2, 0.05, 2.0]
    b = [1.0, 0.4, -0.7, 0.2]
    assert math.isclose(embeddings.cosine_unit(embeddings.unit(a), b),
                        embeddings.cosine(a, b), rel_tol=1e-12)
    assert embeddings.unit([0.0, 0.0]) is None
    assert embeddings.cosine_unit([1.0, 0.0], [0.0, 0.0]) == 0.0


def test_client_is_constructed_once_and_shared(monkeypatch):
    built = []

    class DummyClient:
        def __init__(self):
            built.append(self)

    monkeypatch.setattr(embeddings, "OpenAI", DummyClient)
    monkeypatch.setattr(embeddings, "_client", None)
    first = embeddings._get_client()
    second = embeddings._get_client()
    assert first is second
    assert len(built) == 1


@pytest.fixture
def semantic_ledger(con, settings, monkeypatch):
    """Three facts with deterministic vectors: two paraphrases of one topic
    (nearly identical vectors) and one distinct topic."""
    vectors = {
        "Alex plays weekend chess at the Fairhaven club": [1.0, 0.0, 0.0],
        "Alex is a weekend chess player (Fairhaven club)": [0.999, 0.04, 0.0],
        "Sam is training for a marathon in autumn": [0.0, 1.0, 0.0],
    }
    monkeypatch.setattr(embeddings, "available", lambda settings=None: True)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, s: [vectors[t] for t in texts])
    for content in vectors:
        ledger.add_fact(con, content, settings, source="user",
                        origin_agent="user")
    return vectors


def test_semantic_ranking_and_paraphrase_collapse_survive_the_refactor(
        con, settings, semantic_ledger, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_query",
                        lambda q, s: [0.98, 0.05, 0.0])  # near the chess topic
    got = recall.recall(con, settings, query="chess hobbies", limit=5)
    contents = [f["content"] for f in got]
    # chess outranks marathon, and the two chess paraphrases collapsed to one
    assert contents[0].startswith("Alex")
    assert sum(1 for c in contents if "chess" in c) == 1
    # internal working fields never leak into results
    assert all("_vec" not in f and "embedding" not in f for f in got)


def test_keyword_only_path_still_collapses_paraphrases(
        con, settings, semantic_ledger, monkeypatch):
    """No query vector (keyless): facts still carry embeddings, and the
    paraphrase collapse must keep using them — the keyless fallback."""
    monkeypatch.setattr(embeddings, "embed_query", lambda q, s: None)
    got = recall.recall(con, settings, query="chess club", limit=5)
    contents = [f["content"] for f in got]
    assert sum(1 for c in contents if "chess" in c) == 1  # collapsed, not two
