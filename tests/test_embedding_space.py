"""#60: local embedding endpoint and the space-safe re-embed migration."""

import pytest

from memory_service import db as db_mod
from memory_service import embeddings, recall
from memory_service.config import Settings


@pytest.fixture(autouse=True)
def fresh_client(monkeypatch):
    monkeypatch.setattr(embeddings, "_client", None)
    embeddings._query_cache.clear()


def _settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path / "d", **kw)


class _FakeOpenAI:
    built: list = []
    dim = 4

    def __init__(self, **kwargs):
        _FakeOpenAI.built.append(kwargs)
        outer = self

        class _Embeddings:
            @staticmethod
            def create(model, input):
                class _Datum:
                    def __init__(self, i):
                        self.embedding = [float(i + 1)] * outer.dim

                class _Resp:
                    data = [_Datum(i) for i in range(len(input))]

                return _Resp()

        self.embeddings = _Embeddings()


def test_available_with_base_url_and_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert not embeddings.available(_settings(tmp_path))
    s = _settings(tmp_path, embedding_base_url="http://127.0.0.1:11434/v1")
    assert embeddings.available(s)


def test_base_url_client_gets_placeholder_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(embeddings, "OpenAI", _FakeOpenAI)
    _FakeOpenAI.built = []
    s = _settings(tmp_path, embedding_base_url="http://127.0.0.1:11434/v1",
                  embedding_model="nomic-embed-text")
    vecs = embeddings.embed_texts(["hello"], s)
    assert vecs and len(vecs[0]) == 4
    assert _FakeOpenAI.built == [
        {"base_url": "http://127.0.0.1:11434/v1", "api_key": "local"}]


def _held_vectors(con):
    return con.execute(
        "SELECT COUNT(*) AS n FROM facts WHERE embedding IS NOT NULL"
    ).fetchone()["n"]


def _fact(con, content, vec):
    con.execute(
        "INSERT INTO facts(content, created_at, event_date, content_hash,"
        " embedding) VALUES(?,?,?,?,?)",
        (content, 1700000000.0, 1700000000.0, f"h{content[:8]}",
         embeddings.pack(vec) if vec else None))
    con.commit()


def test_fresh_store_adopts_space_without_wipe(con, settings):
    assert embeddings.sync_space(con, settings) is False
    assert embeddings.stored_space(con) == settings.embedding_model
    assert embeddings.sync_space(con, settings) is False  # steady state


def test_legacy_vectors_are_grandfathered(con, settings):
    _fact(con, "legacy", [0.1, 0.2, 0.3])
    assert embeddings.sync_space(con, settings) is False
    assert _held_vectors(con) == 1
    assert embeddings.stored_space(con) == settings.embedding_model


def test_model_change_wipes_and_restamps(con, settings):
    _fact(con, "one", [0.1, 0.2, 0.3])
    embeddings.sync_space(con, settings)
    settings.embedding_model = "nomic-embed-text"
    assert embeddings.sync_space(con, settings) is True
    assert _held_vectors(con) == 0
    assert embeddings.stored_space(con) == "nomic-embed-text"


def test_reembed_refills_after_model_change(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(embeddings, "OpenAI", _FakeOpenAI)
    s = _settings(tmp_path, embedding_base_url="http://127.0.0.1:11434/v1")
    db_mod.init(s)
    con = db_mod.connect(s.db_path)
    try:
        _fact(con, "one", [0.1, 0.2])
        embeddings.sync_space(con, s)
        s.embedding_model = "nomic-embed-text"
        t = embeddings.start_reembed_if_needed(s)
        assert t is not None
        t.join(timeout=10)
        assert _held_vectors(con) == 1
        blob = con.execute(
            "SELECT embedding FROM facts").fetchone()["embedding"]
        assert len(embeddings.unpack(blob)) == _FakeOpenAI.dim
    finally:
        con.close()


def test_recall_never_scores_a_stale_space_vector(con, settings, monkeypatch):
    # a 3-dim vector left behind vs a 4-dim query must be ignored, not scored
    _fact(con, "the zephyr factory tour", [0.5, 0.5, 0.5])
    monkeypatch.setattr(embeddings, "ensure_fact_embeddings",
                        lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "embed_query",
                        lambda q, s: [0.5, 0.5, 0.5, 0.5])
    out = recall.recall(con, settings, "zephyr factory")
    assert [o["content"] for o in out] == ["the zephyr factory tour"]  # via keywords


def test_query_cache_is_model_scoped(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(embeddings, "OpenAI", _FakeOpenAI)
    s = _settings(tmp_path, embedding_base_url="http://127.0.0.1:11434/v1")
    _FakeOpenAI.dim = 4
    v1 = embeddings.embed_query("hello", s)
    s.embedding_model = "nomic-embed-text"
    _FakeOpenAI.dim = 6
    v2 = embeddings.embed_query("hello", s)
    assert len(v1) == 4 and len(v2) == 6
