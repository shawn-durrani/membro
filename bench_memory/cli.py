"""Command line for the benchmark harness. Run it from membro's repo root:

    python -m bench_memory.cli fetch
    python -m bench_memory.cli run --mock --fixture --membro-repo . \\
        --crossband-repo ../crossband --root /tmp/bench-root --out /tmp/bench-out
    python -m bench_memory.cli report --out /tmp/bench-out

See bench_memory/README.md for every option and what a run costs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def read_keys(env_files, environ) -> dict:
    """The two model keys, from ``--env-file`` files first, then the
    environment. Only these two names are read from a file, and neither
    value is ever printed."""
    keys = {name: environ.get(name, "") for name in KEY_NAMES}
    for path in env_files or []:
        for line in Path(path).expanduser().read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            name, sep, value = line.partition("=")
            name, value = name.strip(), value.strip()
            if sep and name in KEY_NAMES and value:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                keys[name] = value
    return keys


def _cmd_fetch(args) -> int:
    from .longmemeval import dataset, upstream

    try:
        path = dataset.verify(args.variant, args.cache)
        print(f"already downloaded and verified: {path}")
        return 0
    except dataset.DatasetMissing:
        pass
    except dataset.DatasetMismatch as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"downloading {upstream.dataset_url(args.variant)}")
    try:
        path = dataset.download(args.variant, args.cache)
    except dataset.DatasetMismatch as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"downloaded and verified: {path}")
    return 0


def _progress(row, spent):
    status = f"ERROR {row.error}" if row.error else (
        f"facts={row.facts} current={row.current} held={row.quarantined}")
    print(f"  {row.question_id} ({row.question_type}) {row.sessions} sessions "
          f"build {row.build_seconds:.0f}s answer {row.answer_seconds:.0f}s  "
          f"{status}  spent so far ${spent:.4f}", flush=True)


def _cmd_run(args) -> int:
    from .live.env_allowlist import EnvContract
    from .live.launcher import LaunchError
    from .live.run import ManifestConflict, RunConfig, Runner
    from .live.worktree import WorktreeError
    from .longmemeval.dataset import DatasetMismatch, DatasetMissing
    from .safety import UnsafeLiveTarget

    keys = {} if args.mock else read_keys(args.env_file, os.environ)
    if not args.mock:
        missing = [k for k in KEY_NAMES if not keys.get(k)]
        if missing:
            print(f"error: a paid run needs {', '.join(missing)} (from "
                  "--env-file or the environment). Use --mock for a free run.",
                  file=sys.stderr)
            return 2
        if args.budget is None:
            print("error: a paid run needs --budget, in US dollars",
                  file=sys.stderr)
            return 2
    cfg = RunConfig(
        membro_repo=Path(args.membro_repo).expanduser(),
        crossband_repo=Path(args.crossband_repo).expanduser(),
        root=Path(args.root).expanduser(), out=Path(args.out).expanduser(),
        membro_ref=args.membro_ref, crossband_ref=args.crossband_ref,
        mock=args.mock, fixture=args.fixture, variant=args.variant,
        cache=Path(args.cache).expanduser() if args.cache else None,
        n=None if args.all else args.n, seed=args.seed, model=args.model,
        hint=not args.no_hint, budget=args.budget,
        stores=Path(args.stores).expanduser() if args.stores else None,
        jobs=args.jobs, judge=not args.no_judge, credentials=keys)
    mode = "mock (free, offline)" if args.mock else f"paid, budget ${args.budget:.2f}"
    what = "fixture" if args.fixture else args.variant
    size = "all" if cfg.n is None else cfg.n
    print(f"bench_memory run: {what}, n={size}, seed={cfg.seed}, {mode}")
    try:
        report = Runner(cfg, dict(os.environ), on_progress=_progress).run()
    except (UnsafeLiveTarget, LaunchError, WorktreeError, EnvContract,
            ManifestConflict, DatasetMissing, DatasetMismatch) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print_report(report)
    return 0


def _cmd_report(args) -> int:
    path = Path(args.out).expanduser() / "report.json"
    if not path.exists():
        print(f"error: no report at {path}", file=sys.stderr)
        return 2
    print_report(json.loads(path.read_text()))
    return 0


def print_report(r: dict) -> None:
    print()
    print(f"questions {r['questions']}, answered {r['answered']}, "
          f"judged {r['judged']}, failed {len(r['failed'])}")
    if r.get("stopped"):
        print(f"stopped: {r['stopped']}")
    if r["judged"]:
        print(f"accuracy {r['accuracy']:.1%} ({r['correct']}/{r['judged']})")
        for qtype, s in r["by_type"].items():
            print(f"  {qtype:<27} {s['accuracy']:6.1%}  ({s['correct']}/{s['n']})")
        a = r["abstention"]
        if a["n"]:
            print(f"  {'abstention (within the above)':<27} "
                  f"{a['correct'] / a['n']:6.1%}  ({a['correct']}/{a['n']})")
    c = r["cost"]
    print(f"cost ${c['total_usd']:.4f} over {c['calls']} model calls"
          + (f", ${c['per_question_usd']:.4f} a question"
             if c["per_question_usd"] is not None else ""))
    for phase, usd in sorted(c["by_phase"].items()):
        print(f"  {phase:<10} ${usd:.4f}")
    for model, m in sorted(c["by_model"].items()):
        print(f"  {model:<28} ${m['usd']:.4f}  {m['calls']} calls  "
              f"{m['input']} in / {m['output']} out")
    if c["unpriced_models"]:
        print(f"  priced at the fallback rate: {', '.join(c['unpriced_models'])}")
    if c["call_errors"]:
        print(f"  model calls that returned an error: {c['call_errors']}")
    led = r["ledger"]
    print(f"ledger per question: {led['facts_mean']} facts, "
          f"{led['current_mean']} current, {led['quarantined_mean']} held; "
          f"mining failures {led['mining_failures']}")
    build = r["seconds"]["build_mean"]
    print(f"seconds per question: build {'-' if build is None else build} "
          f"(stores reused skip it), answer {r['seconds']['answer_mean']}")
    for qid in r["failed"]:
        print(f"  failed: {qid}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench_memory",
        description="Run LongMemEval against a throwaway crossband and membro.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="download the dataset and check its hash")
    p.add_argument("--variant", default="longmemeval_s")
    p.add_argument("--cache", default=None,
                   help="dataset folder (default ~/.cache/bench_memory/longmemeval)")
    p.set_defaults(func=_cmd_fetch)

    p = sub.add_parser("run", help="run questions on throwaway pairs, then judge")
    p.add_argument("--membro-repo", required=True,
                   help="membro checkout: its .venv runs the throwaway membro")
    p.add_argument("--crossband-repo", required=True,
                   help="crossband checkout: its .venv runs the throwaway crossband")
    p.add_argument("--membro-ref", default="origin/main")
    p.add_argument("--crossband-ref", default="origin/main")
    p.add_argument("--root", required=True,
                   help="an empty folder outside both checkouts for the "
                        "worktrees and data; deleted at the end")
    p.add_argument("--out", required=True,
                   help="where results go; pass the same folder to resume")
    p.add_argument("--mock", action="store_true",
                   help="answer every model call locally: free, offline, no keys")
    p.add_argument("--fixture", action="store_true",
                   help="the four synthetic questions instead of the dataset")
    p.add_argument("--variant", default="longmemeval_s")
    p.add_argument("--cache", default=None)
    size = p.add_mutually_exclusive_group()
    size.add_argument("--n", type=int, default=6,
                      help="questions, spread across types (default 6)")
    size.add_argument("--all", action="store_true", help="every question")
    p.add_argument("--seed", type=int, default=0, help="which sample to draw")
    p.add_argument("--model", default=None,
                   help="the Claude seat's model (default: crossband's own)")
    p.add_argument("--no-hint", action="store_true",
                   help="leave the abstention sentence off the question")
    p.add_argument("--budget", type=float, default=None,
                   help="US dollars; a paid run stops before passing it")
    p.add_argument("--env-file", action="append", default=[],
                   help="read ANTHROPIC_API_KEY and OPENAI_API_KEY from this "
                        "file; nothing else in it is read")
    p.add_argument("--stores", default=None,
                   help="keep each question's built membro here, and reuse it")
    p.add_argument("--jobs", type=int, default=1,
                   help="questions run at once (default 1)")
    p.add_argument("--no-judge", action="store_true", help="skip grading")
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("report", help="print a finished run's report again")
    p.add_argument("--out", required=True)
    p.set_defaults(func=_cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
