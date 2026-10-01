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

One caveat when reading code comments: issue numbers in code that
predates the 2026-08-06 public release refer to a private tracker that
did not come with the repo. Read them as design-history labels, not
links; the reasoning around them stands alone. They are rewritten
opportunistically when a file is touched (#8), and a number can also
name a real issue here by coincidence - `git blame` settles which
tracker a comment meant.

## Rules

- Tests accompany behaviour changes.
- User-visible changes get one new file under `changelog.d/`, not an
  edit to `CHANGELOG.md`. Name it `<issue>-<slug>.md` and write the
  finished entry: one `- ` paragraph in the changelog's voice, with
  continuation lines indented two spaces. Entries fold into the
  changelog at release, so two open PRs never touch the same line.
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
  The script is a byte-for-byte copy of crossband's, the fleet's
  canonical scanner, and `tests/test_secret_scan.py` fails when the copy
  differs. Do not patch it here: land the fix in crossband, then copy
  the file across and commit it, from a local checkout or with
  `curl -fsSL https://raw.githubusercontent.com/shawn-durrani/crossband/main/scripts/secret-scan.sh -o scripts/secret-scan.sh`.
- The scope boundaries in [ARCHITECTURE.md](ARCHITECTURE.md) are
  deliberate.

## Writing documentation

Write a page the way you'd explain membro to a smart friend who's never
seen it, and if you wouldn't say a sentence like that, rewrite it until
you would. Write in Australian English, use contractions, call the reader
"you", and pick short words over long ones. Keep the average sentence
under 18 words, with fewer than one in ten over 35. A paragraph is one
thought and opens with its point. A caveat gets a sentence of its own.
When you bring in something from outside the app, say what it is in a
sentence and link its own documentation. Don't announce a count
before a list, and don't end a paragraph on a line that sounds good.
Keep design metaphors out of [docs/API.md](docs/API.md), so nobody has
to read the design essay to use the wire contract. README.md is the page
to measure against.

`tests/test_doc_style.py` checks the mechanical part. Every markdown
file in the repo is held to the same ceilings: no em-dash, no sentence
over 55 words, no table cell over 45 words, and a heading at least
every 50 lines of prose. A doc rewritten in the voice is listed in
`CONVERTED` in that file, and those docs also keep to these rules:

- no dashes and no semicolons
- one colon per sentence, and only to introduce a list, a command or a
  quoted value
- bracketed asides under eight words, and no sentence starting with one
- capitals only for acronyms
- none of the filler words the test names
- no sentence that announces a count before the list
- contrasts, such as "X, not Y", kept rare
- no history and no issue numbers, which belong in the changelog and
  the issue
- no pointers to the page itself
- no sentence opening with "So" or "Because"

When you rewrite a doc, add its path to `CONVERTED` in the same pull
request, and the suite tells you what's left. Test files are
different, because a test names the issue it guards.

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
- [ ] `python scripts/fold_changelog.py vX.Y.Z` run: `changelog.d/`
      empty, the new section dated, Unreleased left empty above it
- [ ] `__version__` bumped
