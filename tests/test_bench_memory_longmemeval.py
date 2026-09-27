"""LongMemEval in the harness: loading, date shifting, the answers file and
the judge.

The judge's prompts are copied from the upstream scorer, and a score is only
comparable while they stay byte for byte the same. A hash pins them here.
Set LONGMEMEVAL_SRC to a checkout of the pinned upstream commit to also
compare them with the scorer's own function.
"""

import ast
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from bench_memory.longmemeval import contract, dataset, judge, upstream

NOW = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.timezone.utc)


def _raw(qid="q1", qtype="multi-session"):
    return {
        "question_id": qid, "question_type": qtype, "question": "How many?",
        "answer": "3", "question_date": "2023/05/30 (Tue) 12:00",
        "haystack_dates": ["2023/05/20 (Sat) 12:00", "2023/05/29 (Mon) 12:00"],
        "haystack_session_ids": ["s-a", "s-b"],
        "haystack_sessions": [
            [{"role": "user", "content": "one"}, {"role": "assistant", "content": ""}],
            [{"role": "user", "content": "two"}, {"role": "assistant", "content": "ok"}],
        ],
    }


def test_a_conversation_moves_so_its_question_is_asked_now():
    q = dataset.parse([_raw()], now=NOW)[0]
    first, second = (dt.datetime.fromisoformat(s["created_at"].replace("Z", "+00:00"))
                     for s in q.sessions)
    assert (second - first).days == 9          # the gap is kept
    assert (NOW - second).days == 1            # and it ends a day before now
    assert q.shift_days > 1000
    assert q.sessions[0]["turns"] == [{"speaker": "user", "content": "one"}]


def test_unshifted_keeps_the_original_dates():
    q = dataset.parse([_raw()], now=NOW, shift=False)[0]
    assert q.sessions[0]["created_at"] == "2023-05-20T12:00:00Z"
    assert q.shift_days == 0


def test_the_fixture_is_four_synthetic_questions():
    qs = dataset.load_fixture(now=NOW)
    assert len(qs) == 4
    assert sum(q.is_abstention for q in qs) == 1
    assert all(q.sessions for q in qs)


def test_the_sample_covers_every_type_before_repeating_one():
    items = dataset.parse([_raw(f"{t}-{i}", t) for t in ("a", "b", "c")
                           for i in range(5)], now=NOW)
    picked = dataset.stratified_sample(items, 3, seed=1)
    assert sorted(q.question_type for q in picked) == ["a", "b", "c"]
    again = dataset.stratified_sample(items, 3, seed=1)
    assert [q.question_id for q in picked] == [q.question_id for q in again]
    assert len(dataset.stratified_sample(items, None)) == 15


def test_a_missing_or_altered_download_is_refused(tmp_path, monkeypatch):
    with pytest.raises(dataset.DatasetMissing):
        dataset.verify("longmemeval_s", tmp_path)
    monkeypatch.setitem(upstream.VARIANTS, "longmemeval_s", ("s.json", "0" * 64))
    (tmp_path / "s.json").write_text("[]")
    with pytest.raises(dataset.DatasetMismatch):
        dataset.verify("longmemeval_s", tmp_path)


def test_download_writes_the_pinned_file(tmp_path, monkeypatch):
    body = json.dumps([_raw()]).encode()
    monkeypatch.setitem(upstream.VARIANTS, "longmemeval_s",
                        ("s.json", hashlib.sha256(body).hexdigest()))
    urls = []

    def opener(url, timeout):
        urls.append(url)
        return io.BytesIO(body)

    path = dataset.download("longmemeval_s", tmp_path, opener=opener)
    assert path.read_bytes() == body
    assert urls == [f"https://huggingface.co/datasets/{upstream.DATASET_REPO}"
                    f"/resolve/{upstream.DATASET_REVISION}/s.json"]
    assert len(dataset.load("longmemeval_s", tmp_path, now=NOW)) == 1


def test_answers_append_and_resume(tmp_path):
    path = tmp_path / "predictions.jsonl"
    contract.append(path, "q1", "first")
    contract.append(path, "q2", "")
    assert contract.answered(path) == {"q1", "q2"}
    with open(path, "a") as fh:
        fh.write('{"question_id": "q3", "hypo')   # killed mid-write
    assert contract.answered(path) == {"q1", "q2"}
    contract.repair(path)
    contract.append(path, "q3", "third")
    assert [r["question_id"] for r in contract.read(path)] == ["q1", "q2", "q3"]


def test_a_bad_line_mid_file_is_corruption(tmp_path):
    path = tmp_path / "predictions.jsonl"
    path.write_text('{"question_id": "q1", "hypothesis": "a"}\nnot json\n'
                    '{"question_id": "q2", "hypothesis": "b"}\n')
    with pytest.raises(contract.ContractViolation):
        contract.read(path)


# The five templates, hashed. A change here is a change to the benchmark.
TEMPLATES_SHA256 = "140234c31249c1c446f9bdd57492d71ee8a906d9cae8db1a7551ee7c4917aaff"


def _templates():
    return "\x00".join([judge._FACTUAL, judge._TEMPORAL, judge._UPDATE,
                        judge._PREFERENCE, judge._ABSTENTION])


def test_the_judge_prompts_are_pinned():
    assert hashlib.sha256(_templates().encode()).hexdigest() == TEMPLATES_SHA256


@pytest.mark.parametrize("task, marker", [
    ("single-session-user", "a correct answer"),
    ("single-session-assistant", "a correct answer"),
    ("multi-session", "a correct answer"),
    ("temporal-reasoning", "off-by-one"),
    ("knowledge-update", "updated answer"),
    ("single-session-preference", "Rubric"),
])
def test_each_type_gets_its_template(task, marker):
    assert marker in judge.anscheck_prompt(task, "Q", "A", "R")
    assert "unanswerable" in judge.anscheck_prompt(task, "Q", "A", "R", abstention=True)


def test_an_unknown_type_is_refused():
    with pytest.raises(NotImplementedError):
        judge.anscheck_prompt("new-type", "Q", "A", "R")


def test_the_scorers_verdict_rule():
    assert judge.verdict("Yes.") and judge.verdict(" yes")
    assert not judge.verdict("No.")
    req = judge.request("p")
    assert req == {"model": "gpt-4o-2024-08-06", "n": 1, "temperature": 0,
                   "max_tokens": 10, "messages": [{"role": "user", "content": "p"}]}


def test_grading_keeps_no_answer_text_and_skips_what_is_done(tmp_path):
    refs = [{"question_id": "q1", "question_type": "multi-session",
             "question": "Q1", "answer": "A1"},
            {"question_id": "q2_abs", "question_type": "temporal-reasoning",
             "question": "Q2", "answer": "A2"}]
    preds = [{"question_id": "q1", "hypothesis": "secret answer text"},
             {"question_id": "q2_abs", "hypothesis": "I don't know"},
             {"question_id": "stray", "hypothesis": "x"}]
    prompts = []

    def complete(req):
        prompts.append(req["messages"][0]["content"])
        return "yes" if "unanswerable" in prompts[-1] else "no"

    out = tmp_path / "judged.jsonl"
    rows = judge.grade(preds, refs, out, complete)
    assert {k: v["label"] for k, v in rows.items()} == {"q1": False, "q2_abs": True}
    assert rows["q2_abs"]["abstention"]
    assert "secret answer text" not in out.read_text()
    judge.grade(preds, refs, out, complete)
    assert len(prompts) == 2


@pytest.mark.skipif(not os.environ.get("LONGMEMEVAL_SRC"),
                    reason="set LONGMEMEVAL_SRC to an upstream checkout")
def test_the_prompts_match_the_upstream_scorer():
    src = Path(os.environ["LONGMEMEVAL_SRC"]) / upstream.SCORER_RELPATH
    tree = ast.parse(src.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "get_anscheck_prompt")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(src), "exec"), ns)
    for task in ("single-session-user", "single-session-assistant",
                 "multi-session", "temporal-reasoning", "knowledge-update",
                 "single-session-preference"):
        for abstention in (False, True):
            assert ns["get_anscheck_prompt"](task, "Q", "A", "R", abstention) == \
                judge.anscheck_prompt(task, "Q", "A", "R", abstention)
