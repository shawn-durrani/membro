"""Am-I-busy: the in-flight work a restart would interrupt.

The fleet's deploy watcher asks `GET /v1/busy` before it restarts this
service and waits while the answer is true (workbench#69). Each piece of
slow work marks itself for as long as it runs: a job from the registry
(distill, summary, consolidate, viz-embeddings), a backup mid-copy, a
judge pass, the re-embed refill. The route reads these marks and nothing
else, so it never waits on the database and answers in microseconds
whatever the ledger is doing.

Reasons are fixed labels, one word for the kind of work: never an id,
never content. `begin` refuses a label outside LABELS, so no caller can
widen that by accident.
"""

import itertools
import threading
import time
from contextlib import contextmanager

# Every label the route can ever answer with. A job's `kind` doubles as
# its label, so the four registry kinds sit here beside the three marks
# placed by hand.
LABELS = frozenset({
    "distill", "summary", "consolidate", "viz-embeddings",
    "backup", "judge", "reembed",
})

# A mark can outlive its work only when a thread hangs: a provider call
# that never returns, say. The registry lives in memory, so a crash cannot
# leave a mark behind, but a hang would hold every deploy for ever. Past
# this ceiling a mark stops counting, and the restart it stops blocking is
# the cure for the hang.
STALE_AFTER_S = 60 * 60

_lock = threading.Lock()
_marks: dict[int, tuple[str, float]] = {}   # token -> (label, started)
_tokens = itertools.count()


def begin(label: str) -> int:
    """Place a mark and return the token `end` takes. Prefer `mark` unless
    the work starts in one thread and finishes in another, as a job does."""
    if label not in LABELS:
        raise ValueError(f"busy label {label!r} is not declared in busy.LABELS")
    token = next(_tokens)
    with _lock:
        _marks[token] = (label, time.monotonic())
    return token


def end(token: int) -> None:
    with _lock:
        _marks.pop(token, None)


@contextmanager
def mark(label: str):
    """Hold `label` as a busy reason for the duration of the block."""
    token = begin(label)
    try:
        yield
    finally:
        end(token)


def snapshot(now: float | None = None) -> dict:
    """The route's answer: `{"busy": bool, "reasons": [labels]}`. Reasons
    are sorted and deduplicated; a mark past the ceiling is left out."""
    now = time.monotonic() if now is None else now
    with _lock:
        live = {label for label, started in _marks.values()
                if now - started < STALE_AFTER_S}
    return {"busy": bool(live), "reasons": sorted(live)}
