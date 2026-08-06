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
  (gitignored) with patterns for your own names and places. A green
  scan covers key shapes and identifiers only; content must be
  synthetic by construction.
- The scope boundaries in [ARCHITECTURE.md](ARCHITECTURE.md) are
  deliberate.

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
