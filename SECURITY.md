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

Two, with distinct jobs. The browser login is a durable password
(scrypt verifier in the store). The admin bearer token gates first-run
enrolment and password reset, and authenticates MCP/curl callers.
Sessions are opaque server-side ids in httpOnly SameSite=Strict
cookies; logout revokes an id everywhere; the token itself never rides
in a cookie.

## Open vs gated

Open on loopback: `/v1/recall` (six fields, max 50 rows),
`/v1/summary`, `/v1/health`, and the ingest/distill/fact-create flows.
Gated even on loopback: everything reading or writing exact rows
(facts, review, verbatim search, attachments, jobs).

## Known limits

- No rate limiting on loopback.
- A loopback process can read fact content through `/v1/recall`'s
  bounded projection. A hostile local process is outside this app's
  threat model.
- No isolation between OS users beyond file permissions (`data/` is
  0600).

## Operational notes

- First run prints the recovery secret to the terminal. Treat terminal
  output and `data/service.log` as sensitive; redact before pasting
  into issues.
- All of `data/` is sensitive: store, snapshots, and logs.
