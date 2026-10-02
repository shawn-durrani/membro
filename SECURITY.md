# Security

## Reporting

Report a suspected vulnerability privately through GitHub. Open the
Security tab on the repository and choose "Report a vulnerability".
Please don't open a public issue for a security report. One person
maintains Membro, and aims to reply within a few days.

## The trust boundary

Membro's whole job is to serve your personal data, so the first
security question is who can reach its port.

- It answers on loopback by default. The server binds `127.0.0.1`, the
  address only your own computer can reach. It refuses to start on any
  other address without `MEMORY_AUTH_TOKEN` set, and off loopback it
  turns away any request that doesn't carry that token.
- If you widen it, widen it to your own tailnet only.
  `scripts/tailscale-serve.sh` puts the admin page on the devices in
  your [tailnet](https://tailscale.com/docs/concepts/tailnet), the
  private network [Tailscale](https://tailscale.com) makes between them.
  Never open the port to the internet, and never use
  [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel).
- `MEMORY_TRUSTED_HOSTS` lists the names off loopback where a browser
  may sign in. On those names a caller who hasn't signed in reaches the
  lock screen and nothing else, and a signed-in browser reaches the rest.
  A request the browser marks as coming from another site is refused.

## Credentials

Each credential Membro knows has its own job.

- A passkey is the everyday way to unlock the admin page once you've
  enrolled one. It's a
  [WebAuthn](https://developer.mozilla.org/en-US/docs/Web/API/Web_Authentication_API)
  platform authenticator, the kind your device unlocks with Touch ID or
  Face ID. Membro stores only the credential's public key, so a copy of
  the database can't pretend to be it.
- The password is the fallback sign-in. Membro keeps only a scrypt
  verifier for it in the database.
- The admin bearer token, `MEMORY_AUTH_TOKEN`, gates first-run
  enrolment and password resets. It's also how MCP servers and `curl`
  sign in.

Enrolling a passkey needs the owner's credential, a signed-in session
or the token, so it can never happen from the lock screen. A passkey is
tied to the address it was made on. Membro accepts one only for
`localhost` or a host listed in `MEMORY_TRUSTED_HOSTS`, and each of
those is enrolled separately.

A session is an opaque id the server keeps. It travels in an httpOnly
cookie with SameSite set to Strict, and the token itself never goes in a
cookie. The database stores only a SHA-256 hash of each id with its
expiry. A restart signs nobody out, and a copy of the database can't
sign anyone in. Signing out revokes that session everywhere. A password
reset or a passkey removal signs out every other browser, and the one
that did it gets a fresh session. Restoring a snapshot signs out
everyone.

## What's gated and what's open

Every route that reads or writes exact rows needs a credential, even
from your own computer. That covers facts, review, word for word
search, attachments, messages, person records, jobs and the
consolidation sweep. The MCP server keeps the same rule when the
service has a token in `.env`, `config.json` or `config.local.json`. Its
`search_history` tool then works only when that token was passed when
you registered it. With no token in those files, the service makes a
new one each time it starts, and the MCP tool can't check it, so
`search_history` answers anyone who can run it.

These answer a caller on loopback with no credential:

- `/v1/recall`, which returns seven fields and at most 50 rows
- `/v1/summary` and its list of versions
- `/v1/health` and `/v1/busy`
- the ingest watermark for each conversation
- `/v1/disposable-identity`, a check for benchmark harnesses that
  answers "no" on a real install and gives away nothing else
- ingest, distill and creating a fact
- `/v1/backup`
- every `/v1/viz/*` route

Some of the open routes reveal more than you might expect.

- `GET /v1/summary/versions/{id}` returns any stored profile in full.
- `POST /v1/summary/versions/{id}/restore` changes which profile is
  live. It only adds a version, so nothing is lost and you can change it
  back, but it's a write with no credential behind it.
- `POST /v1/summary/regenerate` rebuilds the live profile from the
  current ledger, with no credential, and spends model calls doing it.
  The profile it replaces is kept as a version, so you can get it back.
  Until you do, every model reads whatever the rebuild produced.
  `POST /v1/distill` runs the same rebuild whenever it mines a new fact,
  and it's just as open.
- `GET /v1/viz/recalls` returns the first 200 characters of each recent
  question. That includes your history searches, which are gated
  themselves. The rest of `/v1/viz/*` is geometry, and no viz route
  returns what a fact says, but this one returns your own words.
- `POST /v1/viz/recall_trace` returns no fact text, but it scores every
  current fact against a question, facts bound to one chat included. A
  caller can use it to tell whether a word appears in a fact that recall
  wouldn't show them.

The gate was drawn around exact rows from the ledger. These routes
return something else, so the gate doesn't cover them.

## Known limits

- Nothing limits how fast anyone can make requests, on loopback or
  from a trusted host's sign-in page. Only the cost of checking a
  password slows down guessing one.
- A program on your computer can read what facts say through
  `/v1/recall`, within its seven fields and 50 rows. A hostile program
  on your own computer is outside what Membro defends against.
- Nothing keeps the computer's other user accounts out beyond file
  permissions. The table shows what Membro sets.

| Path | Mode | When |
|---|---|---|
| `data/` and every folder in it | 0700 | every startup, and from creation |
| every file Membro makes in `data/`, backups included | 0600 | from the first byte |
| every file already in `data/` | owner only | every startup |

At startup Membro sets its umask to 0o077, so every file it makes after
that is 0600 and every folder 0700. It also takes group and other access
off anything already in `data/`, and leaves the owner's own access as it
was. That covers the `service.log` launchd creates before the service
starts, and anything you copied in by hand. When a start rolls that log
over, the older copy, `service.log.1`, is 0600 too.

SQLite's `memory.db-wal` and `memory.db-shm` files follow `memory.db` at
0600, because SQLite gives them the database file's own mode. Copies in
the backup mirror keep their snapshot's 0600. The restore, and each
script that writes the database, do the same when they start.

## Running it safely

- With no `MEMORY_AUTH_TOKEN` set, the first run prints the recovery
  secret. It goes to the terminal, or to `data/service.log` when launchd
  runs the service. Treat what the terminal shows, and
  `data/service.log`, as private, and take secrets out before you paste
  either into an issue.
- Everything in `data/` is private: the database, the snapshots and the
  logs.
