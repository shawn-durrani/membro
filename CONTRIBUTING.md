# Contributing

Membro is maintained by one person and built first for their own use.
Issues and pull requests are welcome, and replies can take a while.

## Setup

```sh
git clone https://github.com/shawn-durrani/membro.git
cd membro
./start.sh                      # venv, deps, serve on 127.0.0.1:8901
.venv/bin/python -m pytest -q   # the whole suite, no API keys needed
```

The suite has to pass with no API keys set, because CI runs it keyless.
If a change only works with a key, give it a keyless fallback. If the
service runs under launchd, restart it with this command.

```sh
launchctl kickstart -k gui/$(id -u)/dev.membro.server
```

## How work lands

Every change is a PR linked to its issue. It needs CI green, and it
lands by squash-merge with `Fixes #N` in the message. The issue records
why, and the PR records what changed. Whoever comes next can rebuild the
reasoning from the link between them. Branch from `main`,
never off another open PR. Squash-merging the first would orphan the
second.

Some issue numbers in code comments point at a private tracker from
before the public release on 6 August 2026, and that tracker didn't come
with the repo. Read those numbers as labels, and the reasoning around
them stands on its own. They're rewritten when someone touches the file.
A number can also match a real issue here by chance, and `git blame`
tells you which tracker a comment meant.

## Ground rules

- Tests come with behaviour changes.
- A change a person can see gets one new file under `changelog.d/`, and
  `CHANGELOG.md` stays untouched. Name the file `<issue>-<slug>.md`. The
  check accepts any lowercase words joined by hyphens, and the file has
  to end with a newline. Write the finished entry, which is one `- `
  paragraph in the changelog's voice, with continuation lines indented
  two spaces. Entries fold into the changelog at release, so two open
  PRs never touch the same line.
- No real personal data in any diff. Fixtures use the synthetic roster
  in the PR template, and any name outside it is a question for review.
  Turn on the leak scanner once per clone:

```sh
git config core.hooksPath .githooks
```

  You can copy `secret-scan-local.example` to `.secret-scan-local`,
  which is gitignored, and fill it with patterns for your own names and
  places. The scanner looks for key shapes, for identifiers of a real
  machine or person like tailnet names, home folder paths and email
  addresses, and for the patterns in your own list. It checks your list
  only if you made the file. No scan proves a diff is clean, so write
  content that's made up from the start. A green scan isn't clearance
  to publish.
- `scripts/secret-scan.sh` is a byte for byte copy of crossband's, the
  fleet's one scanner, and `tests/test_secret_scan.py` fails when the
  copy drifts. Don't patch it here. Land the fix in crossband, then copy
  the file across and commit it, from a local checkout or with this
  command.

```sh
curl -fsSL https://raw.githubusercontent.com/shawn-durrani/crossband/main/scripts/secret-scan.sh -o scripts/secret-scan.sh
```

- The scope boundaries in [ARCHITECTURE.md](ARCHITECTURE.md) are
  settled, and that page says why. Read it before you widen one.

## Writing documentation

Write a page the way you'd explain Membro to a smart friend who's never
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

Versions are ordinary semantic versions in the 0.x range, with no
stability promise yet. `memory_service.__version__` is the one place
the version lives. The HTTP `contract_version` moves on its own, and
only when the wire contract in [docs/API.md](docs/API.md) changes.

Before you tag, tick every box.

- [ ] Suite green keyless:
      `env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest -q`
- [ ] `pip-audit -r requirements.txt --strict` clean.
- [ ] `bash scripts/secret-scan.sh --tree` green. The bare command scans
      staged lines only, so at release time it scans nothing and still
      reports clean, and `--tree` is the one that looks.
- [ ] No real personal data in code, tests, docs or fixtures.
- [ ] Screenshots and any demo database come from made-up conversations
      only. That includes conversation titles, because a title
      summarises whatever the chat discussed.
- [ ] `python scripts/fold_changelog.py vX.Y.Z` run, so `changelog.d/`
      is empty, the new section is dated, and the Unreleased heading
      over it stays empty.
- [ ] `__version__` bumped.
