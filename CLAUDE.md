# CLAUDE.md

Instructions for AI sessions working in this repository.

## Process

The pipeline is documented once, in
[CONTRIBUTING.md](CONTRIBUTING.md). Session-specific rules, for
sessions with write access working for the maintainer:

- Merge your own PRs once CI is green; don't wait for human approval.
  The maintainer comments asynchronously. External contributors: the
  maintainer merges yours.
- Never commit directly to `main`; never branch off another open PR.
- Restart the supervised service only with
  `launchctl kickstart -k gui/$(id -u)/dev.membro.server`.

## Safety rules

- The suite and every workflow run keyless. Never add a hard dependency
  on an API key.
- No real personal data in any diff. The fleet synthetic roster: people
  Alex, Sam, Dave, Mateo; place Fairhaven; companies AcmeCo, Initech,
  Globex. The eval corpora keep their own role-named cast, where the
  name states the relationship being tested.
- `data/` is the user's memory. Never read, copy, or quote its contents
  into code, tests, docs, commits, or chat. Debug with disposable
  stores.
- The append-only rules in [ARCHITECTURE.md](ARCHITECTURE.md) are
  invariants: no automated hard deletes, no editing ingested messages,
  no auto-approving quarantined facts.

## Orientation

Read [ARCHITECTURE.md](ARCHITECTURE.md), then
[docs/MEMORY_DESIGN.md](docs/MEMORY_DESIGN.md) and
[docs/API.md](docs/API.md). Open issues hold the active work.
