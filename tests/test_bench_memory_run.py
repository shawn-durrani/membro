"""The benchmark runner: resume, the budget stop, the report and the CLI keys.

The throwaway services are replaced by fakes here, so the loop, the files it
writes and the judge run through the mock meter without starting anything.
"""

import json
from types import SimpleNamespace

import pytest

from bench_memory import cli
from bench_memory.live import launcher, run, session
from bench_memory.longmemeval import dataset
from bench_memory.safety import DISPOSABLE_ACK_ENV, UnsafeLiveTarget

ACK = {DISPOSABLE_ACK_ENV: "1"}


@pytest.fixture
def fake_pairs(monkeypatch, tmp_path):
    """Swap the launcher and the session steps for in-memory fakes."""
    calls = {"started": [], "asked": [], "stores": []}

    def open_workspace(**kw):
        return SimpleNamespace(root=tmp_path / "root", token="t",
                               commits={"membro": "m" * 40, "crossband": "c" * 40})

    def start_pair(ws, key, *, meter_prefix, credentials, seed_store=None, **kw):
        calls["started"].append((key, credentials, seed_store))
        folder = tmp_path / "root" / key
        (folder / "membro-data").mkdir(parents=True, exist_ok=True)
        (folder / "membro-data" / "memory.db").write_text("db")
        return SimpleNamespace(folder=folder)

    def ask(pair, question, hint=True):
        calls["asked"].append(question.question_id)
        if question.question_id == "syn_temporal_0001":
            raise RuntimeError("seat fell over")
        return session.Answer(text=f"answer to {question.question_id}",
                              seat="claude", model="claude-opus-4-8", seconds=1.0)

    monkeypatch.setattr(launcher, "open_workspace", open_workspace)
    monkeypatch.setattr(launcher, "close_workspace", lambda ws, remove=True: None)
    monkeypatch.setattr(launcher, "start_pair", start_pair)
    monkeypatch.setattr(launcher, "stop_pair", lambda pair, ws, remove=True: None)
    monkeypatch.setattr(session, "ingest", lambda pair, q: ["c1"])
    monkeypatch.setattr(session, "mine", lambda pair, ids: {
        "failures": [], "summary_chars": 1200})
    monkeypatch.setattr(session, "ledger_counts", lambda pair: {
        "facts": 30, "current": 28, "quarantined": 2})
    monkeypatch.setattr(session, "ask", ask)
    return calls


def _cfg(tmp_path, **kw):
    base = dict(membro_repo=tmp_path / "m", crossband_repo=tmp_path / "c",
                root=tmp_path / "root", out=tmp_path / "out", mock=True,
                fixture=True, n=4)
    base.update(kw)
    return run.RunConfig(**base)


def test_a_mock_run_answers_judges_and_reports(fake_pairs, tmp_path):
    report = run.Runner(_cfg(tmp_path), env=ACK).run()
    out = tmp_path / "out"
    assert report["questions"] == 4 and report["answered"] == 3
    assert report["failed"] == ["syn_temporal_0001"]
    assert report["judged"] == 3 and report["accuracy"] == 0.0
    assert report["cost"]["total_usd"] == 0.0
    assert report["ledger"]["facts_mean"] == 30.0
    # the mock keys reach the pair, never a real one
    assert all(creds == run.MOCK_KEYS for _, creds, _ in fake_pairs["started"])
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert "seat fell over" in next(r["error"] for r in rows
                                    if r["question_id"] == "syn_temporal_0001")
    assert json.loads((out / "manifest.json").read_text())["identity"]["dataset"] == "fixture"
    assert (out / "report.json").exists()


def test_a_rerun_asks_only_what_is_unanswered(fake_pairs, tmp_path):
    run.Runner(_cfg(tmp_path), env=ACK).run()
    fake_pairs["asked"].clear()
    run.Runner(_cfg(tmp_path), env=ACK).run()
    assert fake_pairs["asked"] == ["syn_temporal_0001"]


def test_a_different_run_cannot_resume_into_the_same_folder(fake_pairs, tmp_path):
    run.Runner(_cfg(tmp_path), env=ACK).run()
    with pytest.raises(run.ManifestConflict):
        run.Runner(_cfg(tmp_path, n=3, seed=9), env=ACK).run()


def test_stores_are_kept_and_reused(fake_pairs, tmp_path):
    stores = tmp_path / "stores"
    run.Runner(_cfg(tmp_path, stores=stores, n=1), env=ACK).run()
    kept = list(stores.iterdir())
    assert len(kept) == 1 and (kept[0] / "store.json").exists()
    assert not (kept[0] / ".bench_memory_disposable").exists()
    fake_pairs["started"].clear()
    run.Runner(_cfg(tmp_path, stores=stores, n=1, out=tmp_path / "out2"), env=ACK).run()
    assert fake_pairs["started"][0][2] == kept[0]


def test_a_paid_run_stops_before_the_budget(fake_pairs, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    # an earlier session spent $0.90 on one question
    (out / "usage.jsonl").write_text(json.dumps({
        "question_id": "earlier", "cost_usd": 0.9, "phase": "build"}) + "\n")
    cfg = _cfg(tmp_path, mock=False, budget=1.0, judge=False,
               credentials={"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"})
    report = run.Runner(cfg, env=ACK).run()
    assert fake_pairs["asked"] == []
    assert "budget" in report["stopped"]


def test_the_report_counts_by_type(tmp_path):
    out = tmp_path
    qs = dataset.load_fixture()
    (out / "predictions.jsonl").write_text("".join(
        json.dumps({"question_id": q.question_id, "hypothesis": "x"}) + "\n" for q in qs))
    (out / "judged.jsonl").write_text("".join(
        json.dumps({"question_id": q.question_id, "question_type": q.question_type,
                    "abstention": q.is_abstention, "label": i % 2 == 0}) + "\n"
        for i, q in enumerate(qs)))
    (out / "usage.jsonl").write_text("".join(
        json.dumps({"question_id": q.question_id, "phase": "build",
                    "model": "claude-haiku-4-5", "cost_usd": 0.25}) + "\n" for q in qs))
    r = run.build_report(out, qs)
    assert (r["judged"], r["correct"], r["accuracy"]) == (4, 2, 0.5)
    assert sum(s["n"] for s in r["by_type"].values()) == 4
    assert r["abstention"]["n"] == 1
    assert r["cost"]["per_question_usd"] == 0.25


def test_keys_come_only_from_their_own_lines(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY='from-file'\nexport OPENAI_API_KEY=\"o-file\"\n"
        "MEMORY_AUTH_TOKEN=never-read\nGITHUB_TOKEN=never-read\n")
    keys = cli.read_keys([env_file], {"ANTHROPIC_API_KEY": "from-env",
                                      "GITHUB_TOKEN": "x"})
    assert keys == {"ANTHROPIC_API_KEY": "from-file", "OPENAI_API_KEY": "o-file"}


def test_a_paid_run_needs_keys_and_a_budget(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    argv = ["run", "--membro-repo", ".", "--crossband-repo", ".",
            "--root", str(tmp_path / "r"), "--out", str(tmp_path / "o")]
    assert cli.main(argv) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert cli.main(argv) == 2
    assert "--budget" in capsys.readouterr().err


def test_a_run_needs_the_acknowledgement(fake_pairs, tmp_path):
    with pytest.raises(UnsafeLiveTarget):
        run.Runner(_cfg(tmp_path), env={}).run()
    assert fake_pairs["started"] == []


def test_a_failed_safety_proof_ends_the_run(fake_pairs, tmp_path, monkeypatch):
    def refuse(*a, **kw):
        fake_pairs["started"].append(a)
        raise UnsafeLiveTarget("token mismatch")

    monkeypatch.setattr(launcher, "start_pair", refuse)
    report = run.Runner(_cfg(tmp_path), env=ACK).run()
    assert len(fake_pairs["started"]) == 1
    assert "token mismatch" in report["stopped"]
