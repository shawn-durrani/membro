# CLAUDE.md

Instructions for AI sessions working in this repository.

## Process

The pipeline is documented once, in [CONTRIBUTING.md](CONTRIBUTING.md);
follow it rather than re-deriving it. What is specific to a session with
write access, working for the maintainer:

- Merge your own PRs once CI is green; don't wait for human approval.
  The maintainer comments asynchronously; the PR plus green CI is the
  gate. (External contributors: this does not apply to you; the
  maintainer merges your PRs.)
- Never commit directly to `main`, and never branch off another open PR.
- Restart the supervised service only with
  `launchctl kickstart -k gui/$(id -u)/dev.membro.server`.

## Safety rules that override convenience

- The suite and every workflow must run keyless. Never add a hard
  dependency on an API key.
- No real personal data in any diff: synthetic roster only (see the PR
  template). When a test needs a "realistic" fixture, invent one.
- `data/` is the user's memory. Never read, copy, or quote its contents
  into code, tests, docs, commits, or chat. Debug with disposable
  stores.
- The append-only rules in [ARCHITECTURE.md](ARCHITECTURE.md) are
  invariants, not defaults: no automated hard deletes, no editing
  ingested messages, no auto-approving quarantined facts.

## Orientation

Read [ARCHITECTURE.md](ARCHITECTURE.md) first, then
[docs/MEMORY_DESIGN.md](docs/MEMORY_DESIGN.md) for the memory model and
[docs/API.md](docs/API.md) for the contract. Open GitHub issues hold the
active work; `gh issue list` is the starting point.
