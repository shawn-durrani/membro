# Contributing

Membro is solo-maintained, built primarily for the maintainer's own
use. Issues and PRs are welcome; response times vary.

## Setup

```sh
git clone https://github.com/shawn-durrani/membro.git
cd membro
./start.sh                      # venv + deps + server on 127.0.0.1:8901
.venv/bin/python -m pytest -q   # full suite, no API keys needed
```

The suite must pass with no keys configured; CI runs keyless. A change
that needs a key needs a keyless fallback. If the service runs under
launchd, restart it with
`launchctl kickstart -k gui/$(id -u)/dev.membro.server`.

## How work lands

Every change is a PR linked to its issue, CI green, landed by
squash-merge with `Fixes #N`. The issue records why, the PR records
what changed, and whoever comes next reconstructs the reasoning from
the link. Branch from `main`, never off another open PR: squash-merging
the first would orphan the second.

## Rules

- Tests accompany behaviour changes.
- User-visible changes get a line in `CHANGELOG.md` under Unreleased.
- No real personal data in any diff. Fixtures use the documented
  synthetic roster (see the PR template); any name outside it is a
  review question. Enable the leak scanner once per clone:

```sh
git config core.hooksPath .githooks
```

  Optionally copy `secret-scan-local.example` to `.secret-scan-local`
  (gitignored) with patterns for your own names and places. The scanner
  has three classes: key shapes, infrastructure identifiers, and
  personal content matched against that local deny-list. The third
  class only runs if you made the file, and it is never a completeness
  proof either way, so content must still be synthetic by
  construction. A green scan is not publication clearance.
- The scope boundaries in [ARCHITECTURE.md](ARCHITECTURE.md) are
  deliberate.

## Writing documentation

Budgets, not taste. `tests/test_doc_style.py` enforces the hard limits;
the rest is review. The house reference is a 15.5-word average sentence
with 4% of sentences over 35 words.

- One claim per sentence. Average under 18 words, and keep sentences over
  35 words under 10% of a document.
- No em-dashes. Australian English. Plain English over jargon.
- Caveats earn their own sentence. Appending a limitation to every claim
  is how the important ones stop reading as important.
- Antithesis ("X, not Y", "rather than", "instead of") is a tool, not a
  cadence. If deleting the "not Y" half loses no information, delete it.
- Never announce your own honesty. "Stated plainly", "the honest reason":
  delete the phrase, keep the fact.
- Issue numbers and bug history go in the CHANGELOG and the issue.
  Reference prose says what is true now.
- Do not narrate a document's own structure or edit history. Nobody read
  the previous version.
- A table cell holds a value and a sentence, not a section.
- Headings every 30 to 50 lines, so a section can be navigated.
- One design metaphor at most, and never in [docs/API.md](docs/API.md):
  a wire contract must not need the design essay read first.

## Releasing

Versions are ordinary semantic versions in the 0.x range: no stability
promise yet. `memory_service.__version__` is the single source, and the
HTTP `contract_version` moves separately, only when the wire contract in
[docs/API.md](docs/API.md) changes.

Before a tag, every box:

- [ ] Suite green keyless: `env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest -q`
- [ ] `pip-audit -r requirements.txt --strict` clean
- [ ] `bash scripts/secret-scan.sh --tree` green. The bare command scans
      only staged lines, so at release time it would scan nothing and
      still report clean; `--tree` is the one that looks.
- [ ] No real personal data in code, tests, docs or fixtures
- [ ] Screenshots and any demo database come from synthetic conversations
      only, including the sidebar: generated titles summarise whatever a
      chat actually discussed
- [ ] `__version__` bumped, CHANGELOG entry dated, fresh `## Unreleased`
      left above it
