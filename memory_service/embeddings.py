"""Embeddings for semantic recall — packed float32 blobs in SQLite, brute-force
cosine (sub-millisecond at ledger scale). Degrades to keyword-only recall when
no provider is available. The provider is the OpenAI SDK default, or any
OpenAI-compatible server named by `embedding_base_url` (#60) — with a base URL
set, no API key is required.

Vectors from different models live in different spaces and must never be
compared (#60). The active space (the embedding model name) is stamped once
in the settings table; `sync_space` drops every stored vector atomically when
the configured model changes, and recall serves keyword-only until the
background re-embed refills them. A mixed-space comparison is the failure
this module now exists to prevent."""

import collections
import logging
import math
import os
import struct
import threading

# Module-top import (#78): the lazy in-function import cost ~370ms on the
# first recall of a process — importing is free of any key requirement, so
# there is no reason to defer it into the request path.
from openai import OpenAI

log = logging.getLogger("memory.embeddings")

SPACE_KEY = "embedding_space"


def _base_url(settings) -> str:
    return (getattr(settings, "embedding_base_url", "") or "").strip()


def available(settings=None) -> bool:
    if os.environ.get("OPENAI_API_KEY"):
        return True
    return settings is not None and bool(_base_url(settings))


# One client per process (#78): a fresh OpenAI() per call meant a new
# TCP+TLS handshake to api.openai.com on EVERY recall — a large share of the
# chat client's measured ~0.9s ambient-recall wait. The SDK client is
# thread-safe and pools connections; ledger's embed threads share it too.
_client = None
_client_lock = threading.Lock()


def _get_client(settings=None) -> OpenAI:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                kwargs = {}
                base = _base_url(settings)
                if base:
                    kwargs["base_url"] = base
                    if not os.environ.get("OPENAI_API_KEY"):
                        # the SDK requires a key string; a local server
                        # never reads it (#60)
                        kwargs["api_key"] = "local"
                _client = OpenAI(**kwargs)
    return _client


def embed_texts(texts: list[str], settings) -> list[list[float]] | None:
    """List of vectors, or None when embeddings are unavailable."""
    if not available(settings) or not texts:
        return None
    resp = _get_client(settings).embeddings.create(
        model=settings.embedding_model, input=texts)
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
# traces the same query seconds later) — a tiny LRU means one API call, not
# two. Keyed by (model, query): a model switch must never serve a vector
# from the previous space (#60).
_query_cache: collections.OrderedDict = collections.OrderedDict()


def embed_query(query: str, settings):
    key = (settings.embedding_model, query)
    if key in _query_cache:
        _query_cache.move_to_end(key)
        return _query_cache[key]
    vecs = embed_texts([query], settings)
    v = vecs[0] if vecs else None
    if v is not None:
        _query_cache[key] = v
        if len(_query_cache) > 32:
            _query_cache.popitem(last=False)
    return v


def ensure_fact_embeddings(con, settings, chunk: int = 128) -> None:
    """Embed any facts without a vector yet (new, edited, or dropped by a
    space change), in bounded chunks so one call never ships the whole
    ledger to the provider at once (#60)."""
    while True:
        rows = [dict(r) for r in con.execute(
            "SELECT id, content FROM facts WHERE embedding IS NULL LIMIT ?",
            (chunk,))]
        if not rows:
            return
        vecs = embed_texts([r["content"] for r in rows], settings)
        if vecs is None:
            return
        for row, vec in zip(rows, vecs):
            con.execute("UPDATE facts SET embedding=? WHERE id=?",
                        (pack(vec), row["id"]))
        con.commit()
        if len(rows) < chunk:
            return


# ---- the embedding space (#60) ----

def space_tag(settings) -> str:
    return settings.embedding_model


def stored_space(con) -> str | None:
    row = con.execute("SELECT value FROM settings WHERE key=?",
                      (SPACE_KEY,)).fetchone()
    return row["value"] if row else None


def _set_space(con, tag: str) -> None:
    con.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SPACE_KEY, tag))
    con.commit()


def sync_space(con, settings) -> bool:
    """Startup guard: make the stored vectors match the configured model.

    Same tag: nothing to do. No tag yet: adopt the configured model —
    a pre-#60 store's vectors can only have come from the model it was
    configured with, so they are grandfathered rather than wiped. A real
    model change drops every vector atomically and stamps the new space;
    from that moment recall is keyword-only until the re-embed refills.
    Returns True when a wipe happened (the caller starts the refill)."""
    tag = space_tag(settings)
    stored = stored_space(con)
    if stored == tag:
        return False
    if stored is None:
        _set_space(con, tag)
        return False
    n = con.execute(
        "SELECT COUNT(*) AS n FROM facts WHERE embedding IS NOT NULL"
    ).fetchone()["n"]
    log.warning(
        "embedding model changed (%s -> %s): dropping %d stored vectors; "
        "recall is keyword-only until the re-embed completes", stored, tag, n)
    con.execute("UPDATE facts SET embedding=NULL")
    _set_space(con, tag)
    con.commit()
    _query_cache.clear()
    return True


def start_reembed_if_needed(settings):
    """Run the space guard; after a wipe, refill vectors on a background
    thread in chunks. Returns the thread, or None when nothing changed.
    If the provider is unavailable the refill simply stops — the
    recall-path ensure_fact_embeddings remains the safety net."""
    from . import db  # lazy: keep module import free of a cycle
    con = db.connect(settings.db_path)
    try:
        wiped = sync_space(con, settings)
    finally:
        con.close()
    if not wiped:
        return None

    def _run():
        c = db.connect(settings.db_path)
        try:
            ensure_fact_embeddings(c, settings)
            log.info("re-embed complete")
        except Exception:
            log.exception("re-embed stopped; recall-path backfill continues")
        finally:
            c.close()

    t = threading.Thread(target=_run, daemon=True, name="re-embed")
    t.start()
    return t
