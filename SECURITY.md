# Security

## Reporting

Report suspected vulnerabilities privately via GitHub: **Security →
Report a vulnerability** on this repository. Please don't open public
issues for security reports. Solo-maintained; acknowledgement usually
within a few days.

## The trust boundary, bluntly

The security question for Membro is: who can reach the port? Everything
your assistant knows about you sits in one SQLite file, and the service
exists to serve it. So:

- **Loopback by default.** The server binds `127.0.0.1`; starting it on
  any other host without `MEMORY_AUTH_TOKEN` set is refused at startup.
- **Widening is tailnet-only, and deliberate.** `scripts/tailscale-serve.sh`
  publishes the UI to your own tailnet devices. Never expose the port to
  the open internet, and never use Tailscale Funnel; there is no public
  mode.
- **`MEMORY_TRUSTED_HOSTS`** controls which non-loopback hosts may reach
  the login surface at all. An anonymous tailnet caller reaches the lock
  screen and nothing else.

## Credentials

Two, with distinct jobs. The everyday browser login is a durable
password (memory-hard scrypt verifier in the store; survives restarts).
The admin bearer token is out of band: it gates first-run enrolment and
password reset, and authenticates MCP/curl callers via
`Authorization: Bearer`. Browser sessions are opaque server-side ids in
httpOnly SameSite=Strict cookies; logout revokes the id everywhere at
once, and the cookie never contains the token.

## What answers without a credential

`/v1/recall` (bounded, six documented fields), `/v1/summary`,
`/v1/health`, and the write-side ingest/distill/fact-create flows,
because a chat client calls them every round. Everything that reads or
writes exact rows (facts, review queue, verbatim search, attachments,
jobs) requires the owner credential even on loopback.

## What these controls do not do

- No rate limiting: a loopback caller can hammer the API.
- A loopback caller can still read fact content through `/v1/recall`'s
  bounded projection; if a process on your machine is hostile, that is
  your machine's threat model, not this app's to solve.
- No isolation between OS users on a shared machine beyond file
  permissions (`data/` is 0600).

## Operational notes

- The startup banner prints the recovery secret on first run; treat
  terminal output and `data/service.log` as sensitive, and never paste
  them into public issues unredacted.
- `data/` in its entirety is sensitive: the store, snapshots, and logs
  all live there.
