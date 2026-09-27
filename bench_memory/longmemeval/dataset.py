"""Load LongMemEval questions: the downloaded dataset, or the synthetic fixture.

The dataset is downloaded into a cache outside the repo (default
``~/.cache/bench_memory/longmemeval/``) and checked against the sha256 pinned
in ``upstream.py``. Nothing from it is committed. ``fixture.json`` beside this
module is a four-question synthetic set in the same shape, so the mock path
runs without the download.

Upstream shape, per question::

    {"question_id": "...",   # an "_abs" suffix marks an abstention question
     "question_type": "temporal-reasoning" | "multi-session" | ...,
     "question": "...", "answer": "...",
     "question_date": "2023/05/30 (Tue) 23:40",
     "haystack_dates": ["2023/05/20 (Sat) 02:21", ...],
     "haystack_session_ids": ["sharegpt_yywfIrx_0", ...],
     "haystack_sessions": [[{"role": "user"|"assistant", "content": "..."}]]}
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import upstream

DEFAULT_CACHE = Path.home() / ".cache" / "bench_memory" / "longmemeval"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixture.json"

# "2023/05/20 (Sat) 02:21": the weekday is decoration and ignored.
_DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s*\([^)]*\)\s*(\d{2}):(\d{2})")


class DatasetMissing(FileNotFoundError):
    """The dataset hasn't been downloaded yet."""


class DatasetMismatch(ValueError):
    """A downloaded file doesn't match the pinned hash."""


@dataclass(frozen=True)
class Question:
    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: str
    sessions: list
    # Days added to every timestamp so the conversation ends now. 0 means
    # unshifted. Recorded because a score can't be read without it.
    shift_days: float = 0.0

    @property
    def is_abstention(self) -> bool:
        # The scorer derives this from the id, so the harness does too.
        return "_abs" in self.question_id

    @property
    def turn_count(self) -> int:
        return sum(len(s["turns"]) for s in self.sessions)


def _parse(stamp: str) -> dt.datetime | None:
    m = _DATE.search(stamp or "")
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc)


def _iso(stamp: str, delta: dt.timedelta) -> str:
    parsed = _parse(stamp)
    if parsed is None:
        return "1970-01-01T00:00:00Z"
    return (parsed + delta).strftime("%Y-%m-%dT%H:%M:00Z")


def shift_delta(question_date: str, now: dt.datetime) -> dt.timedelta:
    """How far to move a conversation forward so its question is asked now.

    LongMemEval's haystacks are dated 2023. Membro ranks facts with a recency
    decay whose half-life is 22.5 to 82.5 days, so a three-year-old fact sits
    in the ledger and can never win a ranking. One delta moves the whole
    conversation, so every gap between sessions is kept and the
    temporal-reasoning questions stay answerable.
    """
    asked = _parse(question_date)
    if asked is None:
        return dt.timedelta(0)
    return now - asked


def dataset_path(variant: str, cache: Path | str | None = None) -> Path:
    if variant not in upstream.VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; expected one of "
                         f"{sorted(upstream.VARIANTS)}")
    filename, _ = upstream.VARIANTS[variant]
    return Path(cache or DEFAULT_CACHE) / filename


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(variant: str, cache: Path | str | None = None) -> Path:
    """The dataset file, checked against its pinned hash."""
    path = dataset_path(variant, cache)
    if not path.exists():
        raise DatasetMissing(
            f"{path} not found. Download it with:\n"
            f"  python -m bench_memory.cli fetch --variant {variant}")
    _, expected = upstream.VARIANTS[variant]
    got = _sha256(path)
    if got != expected:
        raise DatasetMismatch(
            f"{path} has sha256 {got}, not the pinned {expected}. Delete it "
            "and fetch again.")
    return path


def download(variant: str, cache: Path | str | None = None, *,
             opener=urllib.request.urlopen) -> Path:
    """Download one variant at the pinned revision and check its hash."""
    path = dataset_path(variant, cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    with opener(upstream.dataset_url(variant), timeout=120) as resp, \
            open(part, "wb") as out:
        for block in iter(lambda: resp.read(1 << 20), b""):
            out.write(block)
    part.replace(path)
    return verify(variant, cache)


def parse(raw: list, *, now: dt.datetime | None = None,
          shift: bool = True) -> list[Question]:
    """Turn upstream-shaped records into Questions, date-shifted to ``now``."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for q in raw:
        delta = shift_delta(q.get("question_date", ""), now) if shift \
            else dt.timedelta(0)
        ids = q.get("haystack_session_ids") or []
        dates = q.get("haystack_dates") or []
        sessions = []
        for i, turns in enumerate(q.get("haystack_sessions") or []):
            kept = [{"speaker": t.get("role", "user"),
                     "content": t.get("content", "")}
                    for t in turns if (t.get("content") or "").strip()]
            if not kept:
                continue
            sessions.append({
                "session_id": ids[i] if i < len(ids) else f"session-{i}",
                "created_at": _iso(dates[i] if i < len(dates) else "", delta),
                # upstream says `role`; membro's ingest says `speaker`
                "turns": kept,
            })
        out.append(Question(
            question_id=q["question_id"], question_type=q["question_type"],
            question=q["question"], answer=str(q.get("answer", "")),
            question_date=q.get("question_date", ""), sessions=sessions,
            shift_days=round(delta.total_seconds() / 86400.0, 2)))
    return out


def load(variant: str = "longmemeval_s", cache: Path | str | None = None, *,
         now: dt.datetime | None = None, shift: bool = True) -> list[Question]:
    return parse(json.loads(verify(variant, cache).read_text()),
                 now=now, shift=shift)


def load_fixture(*, now: dt.datetime | None = None,
                 shift: bool = True) -> list[Question]:
    return parse(json.loads(FIXTURE_PATH.read_text()), now=now, shift=shift)


def references(questions) -> list[dict]:
    """The reference rows the judge reads, in the scorer's field names."""
    return [{"question_id": q.question_id, "question_type": q.question_type,
             "question": q.question, "answer": q.answer} for q in questions]


def stratified_sample(items, n: int | None, *, seed: int = 0) -> list[Question]:
    """``n`` questions spread across question types, the same for one seed.

    Types are filled in turn, so every type appears before any type doubles
    up. ``None`` returns every question, in dataset order.
    """
    if n is None:
        return list(items)
    buckets: dict[str, list] = {}
    for item in items:
        buckets.setdefault(item.question_type, []).append(item)
    rng = random.Random(seed)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    picked, order = [], sorted(buckets)
    while len(picked) < n and any(buckets[t] for t in order):
        for qtype in order:
            if buckets[qtype] and len(picked) < n:
                picked.append(buckets[qtype].pop())
    return picked
