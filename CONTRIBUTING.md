# Contributing

Membro is solo-maintained and built primarily for the maintainer's own
memory; it is public because the design might be useful to others.
Issues and PRs are welcome; response times vary.

## Development setup

```sh
git clone https://github.com/shawn-durrani/membro.git
cd membro
./start.sh                      # venv + deps + server on 127.0.0.1:8901
.venv/bin/python -m pytest -q   # the whole suite, no API keys needed
```

The suite must pass with no keys configured; CI runs keyless on purpose.
If your change only works with a key present, it needs a keyless
fallback. If the service is installed under launchd, restart it with
`launchctl kickstart -k gui/$(id -u)/dev.membro.server`; a hand-started
process fails loudly while the supervised one keeps running.

## How work lands

Everything ships as a PR linked to its issue, with CI green, and lands
by squash-merge with `Fixes #N` in the description. The trail is the
point: the issue holds why, the PR holds what changed, and whoever comes
next (including a future AI session) reconstructs the reasoning from
that link instead of re-deriving it.

Branch from `main`, never off another open PR: squash-merging the first
PR would orphan the second silently, with a green MERGED badge hiding
the loss.

## Ground rules

- Tests accompany behaviour changes; the suite stays keyless-green.
- User-visible changes get one line in `CHANGELOG.md` under Unreleased.
- No real personal data anywhere in a diff. Fixtures use the documented
  synthetic roster (see the PR template); a name outside the roster is a
  review question by definition. Enable the leak scanner once per clone:

```sh
git config core.hooksPath .githooks
```

  Optionally copy `secret-scan-local.example` to `.secret-scan-local`
  (gitignored) with patterns for your own names and places. A green scan
  clears key shapes and identifiers only; content must be synthetic by
  construction.
- Scope boundaries in [ARCHITECTURE.md](ARCHITECTURE.md) are deliberate;
  proposals that work with them land far more easily than proposals that
  work around them.
