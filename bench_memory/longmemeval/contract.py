"""The predictions file, in the exact shape LongMemEval's scorer reads.

The scorer reads JSONL, one ``{"question_id", "hypothesis"}`` object per line,
and matches each line to a reference question by id. It skips an id it
doesn't know and doesn't require every question to be answered. The harness
writes each answer the moment it has it, so an interrupted run keeps every
answer it paid for and resumes where it stopped.
"""

from __future__ import annotations

import json
from pathlib import Path

QUESTION_ID = "question_id"
HYPOTHESIS = "hypothesis"


class ContractViolation(ValueError):
    """A predictions file the scorer couldn't read."""


def record(question_id: str, hypothesis: str) -> dict:
    if not isinstance(question_id, str) or not question_id:
        raise ContractViolation(f"missing or empty {QUESTION_ID!r}")
    if not isinstance(hypothesis, str):
        raise ContractViolation(f"{HYPOTHESIS!r} for {question_id!r} is not text")
    return {QUESTION_ID: question_id, HYPOTHESIS: hypothesis}


def append(path: Path | str, question_id: str, hypothesis: str) -> None:
    line = json.dumps(record(question_id, hypothesis), sort_keys=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read(path: Path | str) -> list[dict]:
    """Parse the file the way the scorer does, tolerating a torn last line.

    A process killed mid-write leaves a partial final line. That question is
    treated as unanswered and asked again. A bad line anywhere else is
    corruption and raises.
    """
    path = Path(path)
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    out = []
    for i, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            if i == len(lines) - 1:
                break
            raise ContractViolation(f"line {i + 1} is not JSON: {exc}") from None
        if not isinstance(obj, dict) or QUESTION_ID not in obj \
                or HYPOTHESIS not in obj:
            raise ContractViolation(
                f"line {i + 1} needs {QUESTION_ID!r} and {HYPOTHESIS!r}")
        out.append(record(str(obj[QUESTION_ID]), str(obj[HYPOTHESIS])))
    return out


def answered(path: Path | str) -> set[str]:
    return {r[QUESTION_ID] for r in read(path)}


def repair(path: Path | str) -> None:
    """Drop a torn last line before appending, so the next line starts clean."""
    path = Path(path)
    if not path.exists():
        return
    good = read(path)
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in good)
    if text != path.read_text(encoding="utf-8"):
        path.write_text(text, encoding="utf-8")
