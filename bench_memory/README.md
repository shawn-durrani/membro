# Memory benchmark harness

The harness runs the published LongMemEval benchmark against membro and
its chat app, crossband. It gives each question a fresh copy of both apps,
loads that question's chat history into membro, asks crossband the question
in a new chat, and grades the answer with LongMemEval's own grading
prompts. Your real membro and crossband are never touched.

[METHODOLOGY.md](METHODOLOGY.md) says what the score means, lists every
adjustment the harness makes, and holds the results and what they cost.

## What you need

You'll need membro and crossband checked out side by side, each with its
own `.venv` from its `start.sh`. The harness runs from membro's repo root,
with membro's interpreter.

A free run needs nothing else. A paid run needs an Anthropic key and an
OpenAI key, and a budget. Membro's miner, its profile and crossband's
seats use the Anthropic key. Membro's embeddings and the grading use the
OpenAI key.

## Get the dataset

```sh
.venv/bin/python -m bench_memory.cli fetch
```

That downloads the cleaned LongMemEval small set, about 280 MB, into
`~/.cache/bench_memory/longmemeval/`. It comes from a pinned revision on
Hugging Face and is checked against a pinned sha256, so every run reads
the same 500 questions. Nothing from it goes in the repo.

## Run it for free first

A mock run answers every model call inside the harness with a short canned
reply. It exercises everything else for real: both apps start from
worktrees, membro ingests and mines the chats, crossband runs a full chat
round with memory, and the grader runs. It costs nothing, needs no keys and
sends nothing off your computer.

```sh
export BENCH_MEMORY_I_UNDERSTAND_THIS_IS_DISPOSABLE=1
.venv/bin/python -m bench_memory.cli run --mock --fixture \
  --membro-repo . --crossband-repo ../crossband \
  --root /tmp/bench-root --out /tmp/bench-mock
```

`--fixture` uses four made-up questions that ship with the harness, so you
can run it before the download. Drop it to run real questions through the
mock. A mock score is always zero, since the canned seat never knows the
answer. A mock run is for checking that both apps still start, talk to each
other and produce answers.

## A paid run

```sh
.venv/bin/python -m bench_memory.cli run --n 12 --seed 1 --budget 5 \
  --env-file ../crossband/.env \
  --membro-repo . --crossband-repo ../crossband \
  --root /tmp/bench-root --out /tmp/bench-paid
```

`--env-file` reads `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from that file
and ignores every other line. Without it the keys come from your
environment. The keys are never printed or written down.

`--budget` is in US dollars and a paid run won't start without one. Every
model call goes through a meter in the harness, which prices it at list
price from the token counts in the reply. Before each question the harness
checks whether the average question so far would take it past the budget,
and stops if it would. Past the budget, the meter refuses every call.

`--n` picks that many questions spread across the six question types, the
same ones for the same `--seed`. `--all` runs all 500. The budget check
has no average to go on before the first question, so give a budget of at
least a dollar.

## What a run costs

The measured cost is about 35 US cents a question with crossband's default
seat, and about 88% of that is membro mining the question's chat history.
All 500 questions come to about $175.
[METHODOLOGY.md](METHODOLOGY.md#what-it-costs) has the breakdown by model.

A question takes about three minutes, almost all of it mining. `--jobs 4`
runs four questions at once and costs the same.

`--stores DIR` keeps each question's mined membro and reuses it on later
runs, which then pay only for the answer and the grading, about 8 cents a
question. That's how you compare two seat models, or the answer with and
without the abstention sentence, without paying for mining twice. A kept
store is dated to the day it was built, so rebuild stores more than a few
weeks old.

## The output folder

Everything a run makes goes in `--out`. Run the same command again with the
same `--out` and it picks up where it stopped, skipping every answered
question.

| File | What it holds |
|---|---|
| `manifest.json` | What was run: the sample, the seat model, both apps' commits, the pinned upstream versions. |
| `predictions.jsonl` | The answers, in the format LongMemEval's own scorer reads. |
| `results.jsonl` | One row per question: sizes, fact counts, timings, any error. No text. |
| `usage.jsonl` | One row per model call: who made it, the model, tokens and cost. No text. |
| `judged.jsonl` | The grader's verdict on each answer. No text. |
| `report.json` | The summary the command prints. |

`python -m bench_memory.cli report --out DIR` prints a finished run's
report again.

## How it stays away from your real apps

Each of these rules is enforced in code, and the tests in
`tests/test_bench_memory_*.py` hold it.

| Risk | What stops it |
|---|---|
| Your `.env` or `config.local.json` gets read | Both apps run from throwaway git worktrees, which have neither. |
| A setting leaks in from your shell | Each app's environment is built from an empty list, with only the two model keys passed by name. |
| The run writes to your real ledger | The harness writes a random token into each new data folder, and membro must echo it back before any write. |
| Crossband talks to your real membro | Crossband is pointed at the throwaway membro, and the harness checks that it says so before asking anything. |
| A seat answers from the web | Search, voice and GitHub keys never reach the throwaway apps, and each chat has web research off. |
| Teardown deletes the wrong folder | A folder is deleted only while it still holds the run's token. |
| Your fleet's ports get used | Ports 8890 to 8910 are never picked. |

The root folder must be empty and outside both checkouts. Set
`BENCH_MEMORY_I_UNDERSTAND_THIS_IS_DISPOSABLE=1` to confirm that everything
under it is yours to delete.

## Licences

LongMemEval's code and its dataset are both under the MIT licence. The
code's is the repository's `LICENSE` file, and the dataset's is the
licence field on both Hugging Face dataset cards. The licence allows
copying, changing and sharing, as long as the copyright notice travels with
the copy.

The harness downloads the dataset into a cache outside the repo and never
commits any of it. The grading prompts in `longmemeval/judge.py` are copied
word for word from the upstream scorer, with the upstream copyright and
licence notice at the top of the file. The prompts are pinned by a test,
since a score is only comparable with other LongMemEval scores while they
match. With `LONGMEMEVAL_SRC` set to a checkout of the pinned upstream
commit, the test suite also compares them with the scorer itself.

## What it doesn't cover

The harness runs LongMemEval only. LoCoMo, the other memory benchmark
people quote, has no loader or grader here.
