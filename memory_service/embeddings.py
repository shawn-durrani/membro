"""Embeddings for semantic recall — packed float32 blobs in SQLite, brute-force
cosine (sub-millisecond at ledger scale). Degrades to keyword-only recall when
no provider key is available; provider is configurable (OpenAI default)."""

import collections
import math
import os
import struct
import threading

# Module-top import (#78): the lazy in-function import cost ~370ms on the
# first recall of a process — importing is free of any key requirement, so
# there is no reason to defer it into the request path.
from openai import OpenAI


def available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


# One client per process (#78): a fresh OpenAI() per call meant a new
# TCP+TLS handshake to api.openai.com on EVERY recall — a large share of the
# chat client's measured ~0.9s ambient-recall wait. The SDK client is
# thread-safe and pools connections; ledger's embed threads share it too.
_client = None
_client_lock = threading.Lock()


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = OpenAI()
    return _client


def embed_texts(texts: list[str], settings) -> list[list[float]] | None:
    """List of vectors, or None when embeddings are unavailable."""
    if not available() or not texts:
        return None
    resp = _get_client().embeddings.create(model=settings.embedding_model, input=texts)
    return [d.embedding for d in resp.data]


def pack(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes):
    return struct.unpack(f"{len(blob) // 4}f", blob)


def cosine(a, b) -> float:
    # math.sumprod is a C-speed dot product — the pure-Python zip loop this
    # replaces made a single recall scan take seconds and the viz trace's
    # pairwise dedup take tens of seconds.
    dot = math.sumprod(a, b)
    na = math.sqrt(math.sumprod(a, a))
    nb = math.sqrt(math.sumprod(b, b))
    return dot / (na * nb) if na and nb else 0.0


def unit(vec):
    """Unit-normalized copy of `vec`, or None for a zero vector (#78)."""
    n = math.sqrt(math.sumprod(vec, vec))
    return [x / n for x in vec] if n else None


def cosine_unit(u, b) -> float:
    """cosine(q, b) where `u` is the already-unit-normalized q. A recall
    scan compares ONE query against every fact; recomputing the query's own
    norm per fact was ~a third of the scan (#78). Same result as cosine()
    up to float rounding."""
    nb = math.sqrt(math.sumprod(b, b))
    return math.sumprod(u, b) / nb if nb else 0.0


# Query embeddings repeat constantly (the chat recalls a query, the live viz
# traces the same query seconds later) — a tiny LRU means one API call, not two.
_query_cache: collections.OrderedDict = collections.OrderedDict()


def embed_query(query: str, settings):
    if query in _query_cache:
        _query_cache.move_to_end(query)
        return _query_cache[query]
    vecs = embed_texts([query], settings)
    v = vecs[0] if vecs else None
    if v is not None:
        _query_cache[query] = v
        if len(_query_cache) > 32:
            _query_cache.popitem(last=False)
    return v


def ensure_fact_embeddings(con, settings) -> None:
    """Embed any facts without a vector yet (new or edited)."""
    rows = [dict(r) for r in con.execute(
        "SELECT id, content FROM facts WHERE embedding IS NULL")]
    if not rows:
        return
    vecs = embed_texts([r["content"] for r in rows], settings)
    if vecs is None:
        return
    for row, vec in zip(rows, vecs):
        con.execute("UPDATE facts SET embedding=? WHERE id=?", (pack(vec), row["id"]))
    con.commit()
