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
