"""A benchmark run: every question on its own throwaway pair, then the judge.

For each question the runner starts a fresh membro and crossband, fills
membro with that question's haystack, asks crossband the question, writes
the answer down, and deletes the pair. One store per question matters: each
LongMemEval question ships its own haystack of about fifty sessions, and a
shared store would let one question be answered from another's.

Everything a run produces lands in its output folder:

* ``manifest.json``: what was run, fixed when the run starts,
* ``predictions.jsonl``: the answers, in the scorer's format,
* ``results.jsonl``: one content-free row per question,
* ``usage.jsonl``: one row per model call, from the meter,
* ``judged.jsonl``: the judge's verdicts,
* ``report.json``: the summary the CLI prints.

A run stopped for any reason resumes from its output folder. Answered
questions are skipped, and the budget counts what earlier sessions spent.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import __version__
from ..longmemeval import contract, dataset, judge, upstream
from ..safety import DISPOSABLE_ACK_ENV, DISPOSABLE_MARKER_NAME, UnsafeLiveTarget
from . import launcher, session
from .meter import Meter, summarise

MOCK_KEYS = {"ANTHROPIC_API_KEY": "mock-anthropic-key",
             "OPENAI_API_KEY": "mock-openai-key"}


class ManifestConflict(RuntimeError):
    """An output folder holds a different run."""


@dataclass
class RunConfig:
    membro_repo: Path
    crossband_repo: Path
    root: Path
    out: Path
    membro_ref: str = "origin/main"
    crossband_ref: str = "origin/main"
    mock: bool = False
    fixture: bool = False
    variant: str = "longmemeval_s"
    cache: Path | None = None
    n: int | None = None
    seed: int = 0
    model: str | None = None
    hint: bool = True
    budget: float | None = None
    stores: Path | None = None
    jobs: int = 1
    judge: bool = True
    credentials: dict = field(default_factory=dict, repr=False)

    def identity(self) -> dict:
        """The fields a resumed run must share with the one it continues."""
        return {"mock": self.mock,
                "dataset": "fixture" if self.fixture else self.variant,
                "n": self.n, "seed": self.seed, "model": self.model or "",
                "hint": self.hint}


@dataclass
class QuestionRow:
    question_id: str
    question_type: str
    abstention: bool
    sessions: int
    turns: int
    shift_days: float
    facts: int | None = None
    current: int | None = None
    quarantined: int | None = None
    summary_chars: int | None = None
    mining_failures: int = 0
    reused_store: bool = False
    build_seconds: float = 0.0
    answer_seconds: float = 0.0
    seat_model: str = ""
    error: str | None = None


def _check_identity(cfg: RunConfig) -> dict | None:
    path = cfg.out / "manifest.json"
    if not path.exists():
        return None
    old = json.loads(path.read_text())
    if old.get("identity") != cfg.identity():
        raise ManifestConflict(f"{cfg.out} holds a different run "
                               f"({old.get('identity')}); use a new --out")
    return old


def _write_manifest(cfg: RunConfig, commits: dict) -> dict:
    path = cfg.out / "manifest.json"
    ident = cfg.identity()
    old = _check_identity(cfg)
    if old is not None:
        if old.get("commits") != commits:
            raise ManifestConflict(
                f"{cfg.out} was run against {old.get('commits')}, and these "
                f"refs are now {commits}. Resume with the same commits, or "
                "start a new --out.")
        return old
    manifest = {"identity": ident, "commits": commits,
                "harness_version": __version__, "upstream": upstream.pins(),
                "membro_config": launcher.MEMBRO_CONFIG,
                "budget_usd": cfg.budget, "created_at": time.time()}
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def _questions(cfg: RunConfig):
    items = dataset.load_fixture() if cfg.fixture else \
        dataset.load(cfg.variant, cfg.cache)
    return dataset.stratified_sample(items, cfg.n, seed=cfg.seed)


def _store_ok(path: Path) -> bool:
    return (path / "memory.db").exists() and (path / "store.json").exists()


def _save_store(pair_dir: Path, dest: Path, row: QuestionRow) -> None:
    """Keep a built store for later runs. Only after membro has stopped:
    a SQLite file copied mid-write is a corrupt store."""
    tmp = dest.with_name(dest.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(pair_dir / "membro-data", tmp,
                    ignore=shutil.ignore_patterns(DISPOSABLE_MARKER_NAME))
    (tmp / "store.json").write_text(json.dumps(asdict(row), indent=1))
    shutil.rmtree(dest, ignore_errors=True)
    tmp.replace(dest)


class Runner:
    def __init__(self, cfg: RunConfig, env: dict, on_progress=None):
        self.cfg = cfg
        self.env = env
        self.on_progress = on_progress or (lambda *_: None)
        self.lock = threading.Lock()
        self.stopped: str | None = None
        cfg.out.mkdir(parents=True, exist_ok=True)
        self.predictions = cfg.out / "predictions.jsonl"
        self.results = cfg.out / "results.jsonl"
        self.meter = Meter(cfg.out / "usage.jsonl", mock=cfg.mock,
                           budget_usd=cfg.budget)
        self.credentials = MOCK_KEYS if cfg.mock else cfg.credentials

    # A question's likely cost, from the ones already run, so the runner can
    # stop before starting one the budget can't cover.
    def _average_cost(self) -> float:
        per = summarise(self.meter.usage_path)["by_question"]
        return sum(per.values()) / len(per) if per else 0.0

    def _one(self, index: int, question, ws) -> None:
        cfg, meter = self.cfg, self.meter
        with self.lock:
            if self.stopped:
                return
            if cfg.budget is not None and not cfg.mock and \
                    meter.spent + self._average_cost() * cfg.jobs > cfg.budget:
                self.stopped = (f"stopped before {question.question_id}: the "
                                f"next question would likely pass the "
                                f"${cfg.budget:.2f} budget")
                return
        key = f"q{index}"
        meter.questions[key] = question.question_id
        meter.phases[key] = "build"
        row = QuestionRow(question_id=question.question_id,
                          question_type=question.question_type,
                          abstention=question.is_abstention,
                          sessions=len(question.sessions),
                          turns=question.turn_count,
                          shift_days=question.shift_days)
        store = cfg.stores / question.question_id if cfg.stores else None
        seed = store if store is not None and _store_ok(store) else None
        pair = None
        started = time.monotonic()
        try:
            pair = launcher.start_pair(
                ws, key, meter_prefix=f"{meter.url}/{key}",
                credentials=self.credentials, anthropic_model=cfg.model,
                seed_store=seed, env=self.env)
            if seed is None:
                mined = session.mine(pair, session.ingest(pair, question))
                row.mining_failures = len(mined["failures"])
                row.summary_chars = mined["summary_chars"]
            else:
                row.reused_store = True
            row.facts, row.current, row.quarantined = \
                session.ledger_counts(pair).values()
            row.build_seconds = round(time.monotonic() - started, 1)
            meter.phases[key] = "answer"
            answer = session.ask(pair, question, hint=cfg.hint)
            row.answer_seconds = answer.seconds
            row.seat_model = answer.model
            with self.lock:
                contract.append(self.predictions, question.question_id,
                                answer.text)
        except UnsafeLiveTarget as exc:
            # A failed safety proof ends the run, not just the question.
            row.error = f"{type(exc).__name__}: {exc}"[:400]
            with self.lock:
                self.stopped = self.stopped or row.error
        except Exception as exc:  # one bad question never ends the run
            row.error = f"{type(exc).__name__}: {exc}"[:400]
        finally:
            if pair is not None:
                try:
                    launcher.stop_pair(pair, ws, remove=False)
                    if store is not None and seed is None and row.error is None:
                        _save_store(pair.folder, store, row)
                    launcher.stop_pair(pair, ws, remove=True)
                except Exception as exc:
                    row.error = row.error or f"teardown: {exc}"[:400]
        with self.lock:
            with open(self.results, "a") as fh:
                fh.write(json.dumps(asdict(row), sort_keys=True) + "\n")
            if meter.over_budget and not self.stopped:
                self.stopped = f"the ${cfg.budget:.2f} budget is spent"
        self.on_progress(row, meter.spent)

    def run(self) -> dict:
        cfg = self.cfg
        if self.env.get(DISPOSABLE_ACK_ENV) != "1":
            raise UnsafeLiveTarget(
                f"set {DISPOSABLE_ACK_ENV}=1 to confirm everything under "
                f"{cfg.root} is a throwaway you're happy to delete")
        _check_identity(cfg)
        questions = _questions(cfg)
        contract.repair(self.predictions)
        done = contract.answered(self.predictions)
        todo = [(i, q) for i, q in enumerate(questions, start=1)
                if q.question_id not in done]
        self.meter.start()
        ws = None
        try:
            if todo:
                ws = launcher.open_workspace(
                    root=cfg.root, membro_repo=cfg.membro_repo,
                    crossband_repo=cfg.crossband_repo, membro_ref=cfg.membro_ref,
                    crossband_ref=cfg.crossband_ref)
                _write_manifest(cfg, ws.commits)
                if cfg.stores:
                    cfg.stores.mkdir(parents=True, exist_ok=True)
                with ThreadPoolExecutor(max_workers=max(1, cfg.jobs)) as pool:
                    for f in [pool.submit(self._one, i, q, ws) for i, q in todo]:
                        f.result()
            # The pre-question check stops with headroom, so the judge, at
            # about a fifth of a cent a question, still fits.
            if cfg.judge and not self.meter.over_budget:
                try:
                    self.grade(questions)
                except Exception as exc:
                    self.stopped = self.stopped or f"judge failed: {exc}"[:300]
        finally:
            if ws is not None:
                launcher.close_workspace(ws)
            self.meter.stop()
        report = build_report(cfg.out, questions)
        report["stopped"] = self.stopped
        (cfg.out / "report.json").write_text(json.dumps(report, indent=1) + "\n")
        return report

    def grade(self, questions) -> None:
        from openai import OpenAI

        self.meter.phases["judge"] = "judge"
        client = OpenAI(api_key=self.credentials.get("OPENAI_API_KEY"),
                        base_url=f"{self.meter.url}/judge.harness/openai/v1",
                        max_retries=5)

        def complete(req: dict) -> str:
            reply = client.chat.completions.create(**req)
            return (reply.choices[0].message.content or "").strip()

        judge.grade(contract.read(self.predictions),
                    dataset.references(questions),
                    self.cfg.out / judge.RESULTS_NAME, complete)


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def build_report(out: Path, questions) -> dict:
    """Accuracy by question type, what it cost, and how the runs went."""
    wanted = {q.question_id for q in questions}
    verdicts = {r["question_id"]: r for r in _rows(out / judge.RESULTS_NAME)
                if r["question_id"] in wanted}
    results = {}
    for r in _rows(out / "results.jsonl"):
        if r["question_id"] in wanted:
            results[r["question_id"]] = r   # the latest attempt wins
    answered = {r["question_id"] for r in contract.read(out / "predictions.jsonl")}

    by_type: dict[str, dict] = {}
    for v in verdicts.values():
        slot = by_type.setdefault(v["question_type"], {"n": 0, "correct": 0})
        slot["n"] += 1
        slot["correct"] += int(v["label"])
    for slot in by_type.values():
        slot["accuracy"] = round(slot["correct"] / slot["n"], 4)
    abst = [v for v in verdicts.values() if v["abstention"]]
    judged_n = len(verdicts)
    correct = sum(int(v["label"]) for v in verdicts.values())

    usage = summarise(out / "usage.jsonl")
    per_q = [c for qid, c in usage["by_question"].items() if qid in answered]
    ok = [r for r in results.values() if not r.get("error")]
    built = [r for r in ok if not r.get("reused_store")]
    return {
        "questions": len(questions),
        "answered": len(answered & wanted),
        "failed": sorted(qid for qid, r in results.items()
                         if r.get("error") and qid not in answered),
        "judged": judged_n,
        "accuracy": round(correct / judged_n, 4) if judged_n else None,
        "correct": correct,
        "by_type": dict(sorted(by_type.items())),
        "abstention": {"n": len(abst),
                       "correct": sum(int(v["label"]) for v in abst)},
        "cost": {
            "total_usd": round(usage["cost_usd"], 4),
            "by_phase": {k: round(v["cost_usd"], 4)
                         for k, v in usage["by_phase"].items()},
            "by_model": {k: {"usd": round(v["cost_usd"], 4), "calls": v["calls"],
                             "input": v["input"], "output": v["output"]}
                         for k, v in usage["by_model"].items()},
            "per_question_usd": round(sum(per_q) / len(per_q), 4) if per_q else None,
            "calls": usage["calls"], "call_errors": usage["errors"],
            "unpriced_models": usage["unpriced"],
        },
        "ledger": {
            "facts_mean": _mean([r.get("facts") for r in ok]),
            "current_mean": _mean([r.get("current") for r in ok]),
            "quarantined_mean": _mean([r.get("quarantined") for r in ok]),
            "mining_failures": sum(r.get("mining_failures") or 0 for r in built),
        },
        "seconds": {"build_mean": _mean([r.get("build_seconds") for r in built]),
                    "answer_mean": _mean([r.get("answer_seconds") for r in ok])},
    }


def _mean(values) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 1) if vals else None
