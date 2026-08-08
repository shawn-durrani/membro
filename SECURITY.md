# Security

## Reporting

Report suspected vulnerabilities privately via GitHub: **Security →
Report a vulnerability** on this repository. Don't open public issues
for security reports. Solo-maintained; acknowledgement usually within a
few days.

## Trust boundary

The security question is: who can reach the port? The service exists to
serve your accumulated personal data, so:

- **Loopback by default.** The server binds `127.0.0.1`. Starting on any
  other host without `MEMORY_AUTH_TOKEN` is refused.
- **Widening is tailnet-only.** `scripts/tailscale-serve.sh` publishes
  the UI to your own tailnet devices. Never expose the port to the open
  internet; never use Tailscale Funnel.
- **`MEMORY_TRUSTED_HOSTS`** lists the non-loopback hosts allowed to
  reach the login surface. An anonymous tailnet caller reaches the lock
  screen and nothing else.

## Credentials

Three, with distinct jobs. The everyday browser unlock is a passkey
(WebAuthn platform authenticator) once one is enrolled; only the
credential's public key is stored, so a copied store cannot impersonate
it. The durable password (scrypt verifier in the store) is the fallback
login. The admin bearer token gates first-run enrolment and password
reset, and authenticates MCP/curl callers; passkey enrolment
additionally requires an already-unlocked session, so it can never
happen from the lock screen. Sessions are opaque server-side ids in
httpOnly SameSite=Strict cookies; logout revokes an id everywhere; the
token itself never rides in a cookie. A passkey is origin-bound: an
assertion is accepted only for `localhost` or a host listed in
`MEMORY_TRUSTED_HOSTS`, each enrolled separately.

## Open vs gated

Gated even on loopback: everything reading or writing exact rows
(facts, review, verbatim search, attachments, jobs, consolidate).

Open on loopback: `/v1/recall` (six fields, max 50 rows),
`/v1/summary` and its version list, `/v1/health`,
`/v1/disposable-identity` (a probe for benchmark harnesses that
answers "no" on a real install and reveals nothing else), the
ingest/distill/fact-create flows, `/v1/backup`, and every `/v1/viz/*`
route.

Also open on loopback, and worth knowing before you assume otherwise:

- `GET /v1/summary/versions/{id}` returns any stored profile in full.
- `POST /v1/summary/versions/{id}/restore` rewrites which profile is
  live. It is append-only, so nothing is destroyed and the change is
  reversible, but it is a write with no credential behind it.
- `POST /v1/summary/regenerate` rebuilds the live profile from the
  current ledger, with no credential and at the cost of LLM calls. The
  profile it replaces is kept as a version, so the change is
  recoverable, but what every model reads next round is whatever the
  rebuild produced. `POST /v1/distill` does the same rebuild whenever it
  mines a new fact, and is equally open.
- `GET /v1/viz/recalls` returns your recent queries verbatim. The rest
  of `/v1/viz/*` is geometry, and no viz route returns fact content,
  but this one returns your own words.

The gate was drawn around exact ledger rows. These four are not exact
ledger rows, so the gate does not cover them. Stated plainly here
because the alternative is a reader inferring a guarantee that is not
in the code.

## Known limits

- No rate limiting on loopback.
- A loopback process can read fact content through `/v1/recall`'s
  bounded projection. A hostile local process is outside this app's
  threat model.
- No isolation between OS users beyond file permissions. At startup the
  service chmods exactly two things: `data/` to 0700 and `data/memory.db`
  to 0600. Two more happen elsewhere. `data/attachments/` is created and
  chmodded 0700 the first time this process stores or reads an
  attachment, not at startup, so on an install that has never taken one
  the directory does not exist. Each attachment file is chmodded 0600
  when its bytes are first written; storage is content-addressed, so a
  file already on disk is left as it is rather than re-chmodded. Nothing
  else under `data/` is chmodded, so snapshots in `data/backups/` and
  `service.log` keep whatever mode their creator's umask allowed,
  typically 0644. The SQLite WAL and SHM files are not in that group:
  SQLite creates them with the mode of the database file itself, so they
  follow `memory.db` at 0600, and a umask can only clear further bits,
  never add them. Either way, the 0700 directory is what keeps other
  users out.

## Operational notes

- First run prints the recovery secret to the terminal. Treat terminal
  output and `data/service.log` as sensitive; redact before pasting
  into issues.
- All of `data/` is sensitive: store, snapshots, and logs.
