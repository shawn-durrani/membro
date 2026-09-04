# Memory Service API: the HTTP contract, v1.5

The contract between Membro and its clients. Owned by this repo.
Versioned; breaking changes bump the major version. Clients check `contract_version`
at handshake and refuse a major mismatch. The same contract-test suite runs in this
repo's CI (against the real service) and in each client's CI (against the stub).

## Contract rules

- Base URL: `http://127.0.0.1:8901/v1`
- Local-only: the server refuses to bind a non-loopback address unless
  `MEMORY_AUTH_TOKEN` is set (then: `Authorization: Bearer <token>`).
- **1.5 (#93): saves on `POST /facts` may carry `guest_speakers`**: the
  guests in the room when a model saved the fact, as the speaker-class
  values `/ingest` already uses (`guest:<name>`, `guest:unknown`; a list of
  strings, max 12). A stamped save is quarantined with a `guest-present:`
  reason naming the guests. The mined path already holds a guest's words
  because each message names its speaker; this closes the same wall over
  the direct save. The origin trust gate outranks the stamp, and a save
  carrying both stamps keeps its `web-derived:` reason with the guest
  clause appended. Absent field = exactly the 1.4 behaviour.
- **1.4 (#84): four additive fields for a client that reads memory
  back.** `/search` hits carry `web_sources`. `/health` carries
  `browser_origin`. `GET /conversations/{app}/{id}/watermark` reports the
  highest message id held for one conversation. `event_date` is a
  calendar day at the owner's local midnight, whichever writer set it.
  A 1.3 client sees nothing new and keeps working.
- **1.3 (#55): messages on `/ingest` and saves on `POST /facts` may carry
  `web_sources`**: the web domains a tool read in the round that produced
  the message or save (a list of strings, max 20). Stored verbatim beside
  the message. A fact born from a stamped message, or saved with a stamp,
  is quarantined with a `web-derived:` reason naming the domains. A public
  page must not write memory by phrasing a sentence well. The origin trust
  gate outranks the stamp. Absent field = exactly the 1.2 behaviour.
- **1.2 (#33): messages on `/ingest` may carry `speaker_identity`**:
  which person record the sending app believes spoke (`person` slug,
  `confidence` 0..1, `method`: introduced | voice-match | by-elimination
  | owner-correction). Stored verbatim beside the message. A fact born
  from an identified message links to that person when the identity is
  strong: introduced and owner-correction always, voice-match at 0.8+,
  weaker never auto-binds. Absent field = exactly the 1.1 behaviour.
- All bodies JSON. Errors the service raises itself use one envelope:
  `{"error": {"code": "...", "message": "..."}}` with conventional HTTP
  status. Two classes come straight from the web framework and keep its
  native `{"detail": ...}` shape instead: request-schema validation (422,
  a missing or mistyped field; `detail` is then a per-field list) and an
  unmatched route (404 `{"detail": "Not Found"}`). A client that parses
  error bodies should read `error` first and fall back to `detail`. One
  endpoint can return both shapes: `POST /facts` with no `content` key at
  all is a `detail` 422; `POST /facts` with a 3-character `content` is an
  `error` 422.
- Async operations return `202 {"job_id": "..."}`; poll `GET /jobs/{id}`
  (which requires the owner credential; see "Maintenance").

### Always gated, even on loopback

**1.1: some endpoints require the owner credential ALWAYS, even on
loopback.** This is a *separate, stricter* check from the loopback-vs-token
rule above. Sixteen routes carry that always-on check, and these are all of
them:

- `GET /facts`, `GET /review`, and every verb on one existing fact by id:
  `PATCH /facts/{id}`, `/facts/{id}/supersede`, `/facts/{id}/approve`,
  `/facts/{id}/dismiss`, `DELETE /facts/{id}`
- the two bulk ledger verbs: `POST /facts/quarantine`,
  `POST /review/dismiss-all`
- `POST /search`, `POST /consolidate`, `GET /jobs/{id}`
- all four attachment routes: `GET /attachments`,
  `GET /attachments/{id}/file`, `GET /attachments/{id}/preview`,
  `DELETE /attachments/{id}`

No other `/v1` route carries it. Two things sit outside that count without
contradicting it. `GET /` is not on the list yet still varies by credential:
an unauthenticated caller gets the locked page rather than the admin UI, and
no 401. And the loopback-vs-token rule above is a separate gate that governs
the whole surface.

### Open routes

Every other `/v1` route answers an unauthenticated loopback caller, governed
only by the loopback-vs-token rule:

- `/health`, `/disposable-identity`, `/backup`
- `/ingest`, `/distill`, `POST /facts` to create
- `/recall`, `GET /summary`
- **`POST /summary/regenerate`**, which rebuilds the live profile
- **all three `/summary/versions*` routes**, including the one that returns
  a stored profile in full and the one that restores it over the live
  profile
- every `/viz/*` route

The last two bullets are the ones a reader is most likely to expect gated.
See "Owner admin token" below, and "Open on loopback, and what that means"
at the end of it.

## Handshake

`GET /health` →
```json
{
  "status": "ok|degraded",
  "contract_version": "1.5",
  "browser_origin": "http://127.0.0.1:8901",
  "db": {"facts": 0, "messages": 0, "size_bytes": 0, "integrity": "ok",
          "fts_in_sync": true, "last_backup_at": null},
  "capabilities": {"embeddings": true, "miner_model": "claude-haiku-4-5"},
  "detail": {"…admin surface, may change without a contract bump…"}
}
```
The chat client probes this on startup: reachable + compatible → memory features
light up; otherwise it runs memoryless.

`status` is `"degraded"` rather than `"ok"` whenever SQLite's integrity check
(`PRAGMA quick_check`) fails, which is the whole point of a health probe: a
client can decline to write into a damaged file instead of piling on. It is
also `"degraded"` when `fts_in_sync` is false: the FTS index has fallen out
of step with the stored messages (a dropped/recreated index is rebuilt empty
and never refills on its own), which makes every `/search` return zero rows
without erroring. The service detects and repairs this automatically at
startup; `scripts/rebuild_fts.py` does the same for a live instance without a
restart. A client seeing `fts_in_sync: false` should treat search results as
unreliable until it flips back, not as an empty archive.
`browser_origin` (1.4) is the address a browser can open this service
at: the `browser_origin` setting when the operator set one, else
`https://<first trusted host>:<tailscale_port>` when the browser surface
is admitted from a tailnet name, else loopback. A client app that links
a person to the admin surface, such as the message eraser, uses this
instead of guessing a host and port.
`status`, `contract_version`, `browser_origin`, `db` and `capabilities`
are contractual.
`detail` is NOT. It carries the whole internal health dict for the admin
page's health panel, and may change without a contract bump: sqlite version,
journal mode, integrity, size, a facts breakdown of
total/current/superseded/quarantined, message and conversation counts, an
attachments block, last backup, and backups kept. It also carries
`dropped_by_reason`, an in-memory count of extraction-wall drops since the
process started (e.g. `{"system-meta": 3, "builder-process": 11}`). That
count resets on restart and is never a record of *what* was dropped.
Clients should read `db`, never `detail`.

## Owner admin token

`GET /facts`, `GET /review`, and every verb that reads or writes ONE existing
fact by id (`PATCH /facts/{id}`, `POST /facts/{id}/supersede`, `/approve`,
`/dismiss`, `DELETE /facts/{id}`) require a valid credential **unconditionally,
including from 127.0.0.1**. So do the two bulk ledger verbs
(`POST /facts/quarantine`, `POST /review/dismiss-all`), all four attachment
routes, and `POST /search`, `POST /consolidate` and `GET /jobs/{id}`: search
returns verbatim transcript snippets, attachments return file bytes and
document text, and job rows carry operation results, so they are gated the
same way. Those sixteen routes are the whole always-on set, and the list at
the top of this document names each one. Either credential
satisfies the check: `Authorization: Bearer <token>` (the real admin token,
used by the MCP admin server and scripts), or the `mm_admin` session cookie a
browser gets from `POST /login` (an opaque session id, never the token
itself). The reasoning: a sandboxed coding-agent session sharing the
machine's loopback interface must not be able to list exact fact
ids/text/status, read the review queue, or search the raw transcripts with no
credential at all; that would bypass the point of the opt-in admin MCP
capability entirely.

### Three rules, all test-enforced

These survive from earlier revisions of this design:

- **Unauthenticated pages never contain credentials.** `GET /` serves the
  real admin UI only to a caller who is ALREADY authenticated (a valid
  `Authorization` header, or the session cookie). Anyone else gets a minimal
  "locked" page (a form, and nothing else) with no ledger data and no secret
  anywhere in the response. The locked page asks for the owner's password,
  or on first run for a recovery secret plus a new password (see "Owner
  password login" below); the admin token is never accepted there.
- **Sessions are opaque server-side ids.** `POST /login` mints a fresh,
  random, high-entropy session id (`secrets.token_urlsafe(32)`) and records
  its expiry **server-side**, in `app.state.admin_sessions`. The id is never
  derived from or equal to any client-supplied value, so there is no session
  fixation. That store is in-memory, so a restart clears it and every browser
  re-authenticates: deliberate, and it keeps sessions out of the ledger and
  the database entirely. The cookie carries ONLY that
  opaque id. A copy of the cookie is therefore a bounded, revocable
  capability: it expires (`app.state.admin_session_ttl`, 24h by default),
  and `POST /logout` deletes it from the server-side store, which
  invalidates **every** copy of that cookie instantly, not just the one
  presented by the browser that clicked "Log out". The real admin token is
  used ONLY for the `Authorization: Bearer` path (MCP admin server, scripts)
  and the enrolment/reset recovery comparison; it is never itself placed in
  a cookie, so a leaked session id cannot be used to derive or reconstruct
  it, and revoking a session never touches the token or any other session.
- **Exact-row endpoints require the owner credential even on loopback** (the
  list above), as do the bulk ledger verbs, the attachment routes,
  `POST /search`, `POST /consolidate` and `GET /jobs/{id}`. The
  loopback-vs-token rule at the top of this document governs every other
  route, including the summary-version and `/viz/*` routes described under
  "Open on loopback, and what that means" at the end of this section.

### Getting the token

Always out-of-band, never over HTTP:
- If `MEMORY_AUTH_TOKEN` is configured (`.env` / `config.local.json` /
  environment), that's the token: **stable across restarts**, which is what a
  persistently-registered MCP client needs. The owner already knows it (they
  set it).
- Otherwise the server mints a fresh random token for its process lifetime
  and **prints it to its own stdout at startup** (the terminal that ran
  `start.sh`), never to an HTTP response and never to a file. A sandboxed
  session sharing the machine's filesystem/network has no route to another
  process's live terminal output.

### Owner password login

The everyday browser login is a durable
**password**, not the admin token, so an owner does not have to hunt the
terminal for a token after every restart. The admin token keeps two roles: it
is still the `Authorization: Bearer` credential (MCP/curl), and it is the
out-of-band **recovery secret** that gates enrolment and reset. It is no
longer accepted as the everyday login.

- The password is stored only as a memory-hard **scrypt verifier** (salt +
  parameters + derived hash; never the password, nothing reversible) in the
  durable local `settings` table. It therefore survives restarts. No schema
  change: an existing database simply has no verifier yet and falls into
  first-run enrolment.
- `POST /login` with form field `password=<value>`, checked against the
  verifier. A correct password mints an opaque session id and sets `mm_admin`
  (`HttpOnly`, `SameSite=Strict`; opaque server-side id, not the token;
  expires; cleared on restart), then redirects to `/`. A wrong password (or
  the admin token submitted here) gets the locked page again, no session
  created. Before enrolment there is nothing to check against, so login
  simply fails and the page offers enrolment.
- `POST /enroll` with `recovery=<admin token>`, `password`, `confirm`: first
  run only (409 once a password exists). The recovery secret is the gate that
  stops an unauthenticated caller sharing loopback (a sandboxed coding agent)
  from self-enrolling: it never sees the terminal/`.env` secret.
  Wrong/missing recovery → 401, nothing written. Passwords must match and be
  ≥ 8 chars. On success the verifier is persisted and the browser is logged
  straight in.
- `POST /reset` with `recovery=<admin token>`, `password`, `confirm`: the
  same recovery-gated proof, allowed at any time, replacing the verifier.
- `POST /logout` revokes the session server-side and clears the cookie.

### Passkey login

With a passkey enrolled, the lock screen offers it
first and the password moves one click behind it; a successful assertion
mints exactly the same opaque session as a password login. The password and
recovery secret are unchanged. A passkey is bound to the web origin it was
created on, so `localhost` and each trusted host enrol separately, and an IP
origin (`127.0.0.1`) can never hold one.

- `POST /webauthn/register/options` and `POST /webauthn/register` (both
  require an unlocked session or the bearer token): start and finish
  enrolment for the origin the page is open on. Only the credential id and
  public key are stored, beside the password verifier in the `settings`
  table; the private key never leaves the authenticator.
- `POST /webauthn/login/options` and `POST /webauthn/login` (lock-screen
  surface): challenge out, signed assertion in, verified against the
  enrolled public key with user verification (Touch ID / Face ID) required.
  The options response discloses no credential ids. Credentials are
  enrolled as discoverable, so the browser finds its own.
- `GET /webauthn/credentials` and `DELETE /webauthn/credentials/{id}`
  (unlocked session or bearer): list and remove enrolled passkeys. Removal
  can never lock the owner out; the password always remains.

These endpoints are the browser admin UI surface (`include_in_schema=False`),
not part of the versioned `/v1` contract, so `contract_version` is unchanged.

### Runtime sequence for the opt-in admin MCP server

(`memory_service.mcp_admin_server`):
1. Configure a stable token: put `MEMORY_AUTH_TOKEN=<random value>` in `.env`
   (or `config.local.json`).
2. Restart the service so it picks up that value as `app.state.admin_token`
   (an already-running process keeps whatever token, configured or ephemeral,
   it minted at its own startup; the same restart also makes the value usable
   as the browser UI's enrolment/reset recovery secret).
3. Register the MCP server with the **same** token and the service's reachable
   URL. Pass both with `-e`, so they reach the server when it later runs; a
   shell env prefix in front of `claude mcp add` sets them only for the
   registration command, and the registered server then starts without them:

   ```sh
   claude mcp add -s user membro-admin \
     -e MEMORY_AUTH_TOKEN=<token> \
     -e MEMORY_API_URL=http://127.0.0.1:8901/v1 \
     -e PYTHONPATH=<repo> -- \
     <repo>/.venv/bin/python -m memory_service.mcp_admin_server
   ```
4. Validate with one harmless read: `search_facts("")` or `review_queue()`
   should return real rows (or "No matching facts."), not an auth error.

A user-provided correction always outranks conflicting model-mined history:
when `search_facts`/`review_queue` surface older mined facts that contradict
something the owner has since stated directly, the newer explicit correction
is authoritative. These tools show provenance (`origin_agent`) and status
precisely so a remediation session can propose a supersession; repeated or
elaborate mined detail is never, by itself, a reason to prefer it over the
owner's word.

### Open on loopback, and what that means

Four surfaces outside the gated set are more open than a reader of the
paragraphs above would guess. They are stated here rather than left to be
discovered:

- **`GET /v1/summary/versions/{id}` returns a stored profile in full**, text
  and all, to any unauthenticated loopback caller. `GET /summary` is open by
  the same rule and returns the *current* profile, so this widens the reach
  from "the profile now" to "any profile this database has ever generated".
- **`POST /v1/summary/versions/{id}/restore` is a write**, and it is open on
  loopback. Any local process can make an older profile the live one. It is
  append-only, so nothing is destroyed and you can restore back, but the
  profile every model reads next round can be changed with no credential.
- **`POST /v1/summary/regenerate` rewrites the live profile**, and it is
  open on loopback. Any local process can make the service rebuild the
  profile from the current ledger, which spends LLM calls and replaces the
  text `GET /summary` serves from then on. The replaced profile is kept as
  a version row, so it can be restored, but the rebuild itself needs no
  credential. `POST /v1/distill` runs the same rebuild whenever it mines a
  new fact (`regenerate_summary` defaults to true) and is equally open.
- **`GET /v1/viz/recalls` returns your questions verbatim** (`query`, first
  200 characters, from the append-only access log), open on loopback. Fact
  content never travels through it, but the queries are your own words, so
  it is not geometry in the sense the rest of `/viz/*` is.

These are the code's behaviour as it stands, recorded here so the document
does not promise a gate the service does not implement.

## Episodic record

`POST /ingest`: append transcript messages (idempotent on `(source_app, external_id)`).
```json
{"source_app": "multi-model-chat", "conversation_id": "chat-123",
 "title": "Weekend plans",
 "messages": [{"external_id": "m-1",
                "speaker": "user|<slug>|guest:<name>|guest:unknown",
                "content": "...", "created_at": "2026-07-04T10:00:00+10:00",
                "attachments": [{"filename": "notes.txt", "mime": "text/plain",
                                  "data_b64": "..."}]}]}
```
→ `{"ingested": 12, "skipped": 3, "attached": 1}`

`title` is the conversation's human label. Optional, and re-applied on every
later ingest of the same conversation, so a chat renamed in the client catches
up here. It is the title `/search` hits carry back and the admin pages show;
a client that never sends one leaves every conversation blank.

### Guest speaker classes

Additive, 2026-08-08. Beside `user` (the owner) and a bare model slug
(`claude`), a message's `speaker` may carry `guest:<name>`
for another, named human in the session (a multi-human voice session in
room mode), or `guest:unknown` for a human turn whose voice diarization
could not attribute confidently. `source_app` handling and conversation
identity are untouched. What the classes mean downstream, in the mining
pass:

- A fact the miner draws from guest-attributed speech is quarantined by
  default: written, then held in the review queue with the guest's name in
  the stated reason, the same posture `mcp:*` writes get from the write
  gate. `guest:unknown` speech quarantines unconditionally.
- Pronouns resolve per speaker. A guest's first-person statement is a fact
  about the guest, phrased into the owner's ledger in third person, never
  absorbed into the owner's first-person profile. Guests get no profile of
  their own: this service stays single-owner, and a guest's facts exist
  only as facts about the owner's world. A fact about the owner asserted
  by a guest is still guest-provenance and quarantines the same way.
- Fail safe: any other class-prefixed speaker value (say `agent:scribe`)
  is unrecognised and treated as untrusted, quarantining exactly like
  guest speech, so a newer client can never mint trust by inventing a
  class. Facts drawn from the owner's own `user` turns are unchanged.
- Provenance is recorded only where it is real (2026-08-12). A mined fact
  carries `source_message_id` only when it was actually tied to one turn;
  a fact the miner could not bind is stored unbound (`source_message_id`
  null) rather than pinned to whichever turn ended the mining window. In a
  guest-present window such a fact is still held for review, unchanged.
  When the miner supplies a missing binding on the corrective retry, that
  answer is now checked against the turn it names: a turn sharing none of
  the fact's wording, or one no more plausible than a guest's turn in the
  same window, is refused and the fact stays held. A retry's guess about
  who spoke can no longer promote a guest's sentence into owner canon.

### Attachments on ingest

`attachments` (additive, 2026-07-11) is optional, per message. Files are part
of the episodic record: bytes stored whole (content-addressed under
`data/attachments/`), text extracted (text/* fully; PDFs via pypdf,
best-effort) into FTS so `/search` and mining see it; hits from files carry
`speaker: "file: <name>"`. Attachments attach even to already-ingested
(skipped) messages, so backfilling old conversations is a plain re-ingest.
Append-only and immutable like messages; the `attached` count is new rows
(idempotent re-sends count 0).

Limits: ≤5000 messages per call (more is a 413), ≤20 attachments per message
(422), ≤~25 MB decoded per file (422). These bound one request, not your
history: storage itself is uncapped by design, so send a long backlog in
batches rather than one giant POST.

### Distill

`POST /distill`: run the reflection pass (mining + walls) over un-mined ingested
content for a conversation. Async.
```json
{"source_app": "multi-model-chat", "conversation_id": "chat-123"}
```

### Verbatim search

`POST /search`: verbatim FTS over the episodic record. **Requires the owner
credential (`Authorization: Bearer` or the admin session cookie), even on
loopback**: search returns verbatim transcript snippets, which are at least
as revealing as the exact-row ledger reads gated above.
```json
{"query": "...", "limit": 20}
```
→ `{"hits": [{"conversation_id": "...", "title": "...", "speaker": "...",
              "content": "...", "created_at": "...",
              "web_sources": ["example.com"]}]}`
`limit` defaults to 20, maximum 500 (422 outside 1–500). `title` is the
conversation's title as last ingested (empty string if the client never sent
one); `content` is an FTS snippet with `>>match<<` markers, not the whole
message. `web_sources` (1.4) is the list stored with the message on
ingest, empty for a turn that read no web page and for hits from files.
A client that shows a hit to a model should mark a stamped hit as
untrusted, the same way it marks a live fetch.

### Ingest watermark

`GET /conversations/{source_app}/{conversation_id}/watermark` (1.4). Open
on loopback like `/health`; it carries ids and a count, never content.
→ `{"highest_external_id": "412", "messages": 87}`
`highest_external_id` is the largest message `external_id` this service
holds for that conversation, compared numerically when every id is an
integer string and as text otherwise, or `null` when none is held. A
message the owner erased still counts: its id sits in the erasure
journal, and a client that wound back past it would re-send the exact
message just erased. 404 in the standard envelope when the conversation
is unknown here. A client that keeps its own "ingested up to" mark
compares the two on each handoff and winds its mark back when this
service has less, which is what a restore from a snapshot leaves behind.

## Ledger

`POST /facts`: save one fact.
```json
{"content": "...", "event_date": "2026-07-04", "confidence": "high|medium|low",
 "origin_agent": "user | <participant-slug> | mcp:<client>",
 "source_app": "<registered app>",
 "web_sources": ["<domain>", "..."],
 "guest_speakers": ["guest:<name>", "guest:unknown"]}
```
`content` is whitespace-collapsed first, then must be 8–10 000 characters;
anything shorter or longer is a 422 ("nothing meaningful to save" / "fact too
long"). `event_date` is a calendar day (1.4): send `YYYY-MM-DD`, or a
full timestamp and the service keeps only the day it falls on in the
owner's local time. It is stored as that day's local midnight, so two
facts about one day compare equal and recall breaks the tie on the save
time. Omit it and the fact is dated to the day it was saved, which is
how invariant 5 holds (`event_date` is never null).
`source_app` is what the gate reads below; `confidence` defaults to `high`
and `origin_agent` to `user`.

`web_sources` (1.3) and `guest_speakers` (1.5) are optional stamps, both
defaulting to empty. `web_sources` lists the web domains read in the
round that produced the save (max 20). `guest_speakers` lists the guests
in the room when a model made the save, as `/ingest` speaker values (max
12): `guest:<name>` for one confidently identified human besides the
owner, or `guest:unknown` for a human who could not be identified. Both
are normalised the same way: entries are stripped, empties dropped,
duplicates removed, order kept. A `guest_speakers` entry that is not a
guest class (a model slug, `user`, an unknown prefix) is dropped, not
rejected. A save carrying either stamp is held for review: `web-derived:`
names the domains, `guest-present:` names the guests in plain English
(`guest:unknown` reads as "an unidentified guest"; at most five are
named). When both are present the reason keeps the `web-derived:` prefix
and the guest clause follows after `; `. The origin gate below outranks
both stamps.

Gate (invariant 4; the gate applies to the write itself, whoever the writer
is): a write reaches canon only if `origin_agent` is `user` **or** it
declares a registered `source_app` (the trusted set). Anything else is
created quarantined (`review: held`). An `mcp:*` origin is **never** trusted:
even paired with a registered `source_app` it stays held, so an adapter (or
any caller spoofing that origin over the local API) cannot launder a fact
into canon by naming a trusted app. A held write also has its `confidence`
forced to `low`, so a caller that sent `high` reads back `low`: an unreviewed
claim is never presented as confident. Response includes
`{"id": 1, "quarantined": bool}`.

To deliberately **stage a fact for owner review** via the API (e.g. an agent
proposing a revision), POST it with a non-`user` `origin_agent` (the authoring
model's slug) and no trusted `source_app`; it lands in `GET /facts?status=quarantined`
for the owner to `approve` or `dismiss`.

### Reading the ledger

`GET /facts?status=valid|superseded|quarantined|all&q=...&limit=...`: list/filter.
**Requires the owner admin token, even on loopback** (see "Owner admin token" above).
`status` defaults to `valid`; `q` is a substring match on content; `limit`
defaults to 100, maximum 1000.
Since 2026-07-27, a `q` beginning with `#` followed by ids (one id, or
several separated by commas or spaces) is an **id lookup** instead of a
content search; the leading `#` is what distinguishes it, because a bare
number is a legitimate thing to search the text for (a year, a figure).
Unknown ids are simply absent from the result, not an error, and an id list
is never trimmed by `limit` (an explicit list asks for exactly those rows;
trimming it would read as "those ids don't exist"). `status` still applies,
so look up a held fact with `status=all`. `status` is not validated server-side: only
`valid`, `superseded` and `quarantined` filter anything, and ANY other value
applies no filter and returns every row. That is how `all` works, and it
means a typo (`quarantied`) silently returns everything rather than a 422, so
check the spelling before trusting a count.

`PATCH /facts/{id}`: edit content/event_date/confidence (re-embeds) and, additively
since 2026-07-11, `importance`; the human outranks the miner on how a fact ages.
`importance` must be an integer 1-10: anything outside that range is **rejected
with a `detail` 422, not clamped**, so send a value in range rather than
relying on the endpoint to fold it back. **Requires the owner admin token,
even on loopback.**

### Changing a fact's state

`POST /facts/{id}/supersede`: `{"successor_id": 2}`; temporal validity, never delete.
**Requires the owner admin token, even on loopback.**

`POST /facts/{id}/approve`: un-quarantine (human only). **Requires the owner
admin token, even on loopback.**
`POST /facts/{id}/dismiss`: reviewed-and-kept-out, non-destructive (human only).
**Requires the owner admin token, even on loopback.**
`POST /review/dismiss-all`: additive since 2026-07-27, the bulk twin of
`/facts/{id}/dismiss`. Every fact currently in the review queue is
dismissed in one call, same non-destructive semantics (stays quarantined and
in the ledger; any one is reversible with `/approve`). Response is
`{"dismissed": <count>}`. Meant for clearing a backlog made stale by a
filtering fix, not routine triage. **Requires the owner admin token, even on
loopback.**
`POST /facts/quarantine`: additive since 2026-07-27, quarantines facts
that were ALREADY accepted as canon. `{"ids": [1, 2], "reason": "..."}`; `reason`
is required and non-empty (an unexplained row in the review queue can't be
adjudicated). Response is `{"quarantined": [...], "skipped": [...]}`; unknown
and already-quarantined ids are skipped, not errors, so a re-run is a no-op.
The affected facts leave `/recall` and the summary, stay in the ledger, and
appear in `GET /review`; each is reversible with `/facts/{id}/approve`. This is
the third treatment between `supersede` (asserts a replacement fact, which a
malformed row does not have) and `DELETE` (destructive). **Requires the owner
admin token, even on loopback.**
`DELETE /facts/{id}`: one of the three human erasers (facts / attachments /
messages); human-initiated only, never automated. Every erasure appends a
content-free row (kind, refs, when - never content) to the `erasures` journal
(#45). **Requires the owner admin token, even on loopback.**

### The review queue

`GET /review`: held-for-review queue. **Requires the owner admin token, even
on loopback.**

Each row is the whole fact row plus `source` (additive, 2026-08-12, contract
version unchanged): the turn the fact was mined from, so the first question a
reviewer has, who said this, is answered without leaving the queue.
```json
"source": {"message_id": 812, "speaker": "guest:Sam", "speaker_class": "guest",
           "created_at": 1754616000.0, "excerpt": "I hate coriander…",
           "truncated": false}
```
Each row also carries `reason_class` (additive, 2026-08-14, #34): a stable
token derived from the hold reason's prefix (`guest-attribution`,
`guest-present`, `speaker-trust`, `grounding`, `temporal`, `source-trust`,
`source-trust-judged`, `source-deleted`, `importance`, `web-derived`,
`external-write`, else `other`; a multi-flag reason classes by its first
flag). The admin page groups the queue by it, and
`POST /facts/bulk-approve` / `POST /facts/bulk-dismiss` (owner credential,
body `{"ids": [...]}`) act on explicit id lists - only ids currently in the
queue are touched, everything else is skipped, not an error, so one decision
clears a whole cause without ever sweeping rows the owner has not seen.

`source` is `null` whenever the fact names no source message: an external
(`mcp:*`) write, a fact saved by hand, or a mined fact that could not be tied
to a single turn (see the guest-speaker notes above). It is never filled in
with a nearby turn. "Sam said this" and "no one knows who said this" are
different decisions, and a queue that blurs them is worse than one that stays
silent. `excerpt` is the first 400 characters of the message's own content
(attachment text is not included); `truncated` says whether more exists.
`speaker_class` is the classification mining uses (`owner`, `model`, `guest`,
`guest-unknown`, `unrecognised`).

## Recall & summary

`POST /recall`: hybrid semantic + keyword + bounded recency; paraphrase dedup;
quarantined always excluded; superseded excluded unless `include_superseded`.
`limit` is capped at 50: recall is a retrieval aid, and an
unbounded top-N over an empty query amounted to a whole-ledger export. The
response carries exactly the fields below and no others: this endpoint
answers unauthenticated loopback callers by design, so its projection is a
security boundary. Adding a field here is a contract change.
```json
{"query": "...", "limit": 10, "include_superseded": false, "origin": "http"}
```
→ `{"facts": [{"id": 1, "content": "...", "event_date": "...", "confidence": "...",
               "origin_agent": "...", "score": 0.87}]}`

`limit` defaults to 10, maximum 50 (422 outside 1–50). `origin` is an
access-log label ONLY; it changes nothing about what comes back: `http` (the
default), `auto` for an ambient recall a client fired on the user's behalf
rather than a model deliberately reaching in, or `mcp:<client>`. It is what
the `/math` live view reads to tell "prepared context" from "went deep"; see
`GET /v1/viz/recalls` below.

An empty `query` skips scoring entirely and returns the most recent
non-quarantined facts, newest first, which is useful as a cheap "what do you
know about me lately". Those rows carry no `score` field at all.

The response is exactly the projection in the example and nothing more:
`id`, `content`, `event_date`, `confidence`, `origin_agent`, and `score`
(the last on scored recalls only; an empty-query recall omits it). That set
is `RECALL_FIELDS` in `api.py`, and a test fails if a field is added without
amending this contract. The rest of the fact row does NOT travel over this
endpoint: no `created_at`, `importance`, `source`, `conversation_id`,
`source_message_id`, `content_hash`, `invalidated_at`, `superseded_by`,
`quarantined_at`, `quarantine_reason` or `review_dismissed_at`.

One consequence worth planning around: with `include_superseded: true` the
superseded facts come back, but with **no field that marks them as
superseded**. `invalidated_at` and `superseded_by` are both outside the
projection, so an HTTP client cannot tell a retired fact from a current one.
The MCP adapter can render its `[SUPERSEDED …]` flag because it calls
`recall.recall()` in-process and sees the whole row; an HTTP caller that
needs lifecycle state must use the owner-gated `GET /facts` instead.

### Summary

`GET /summary` → `{"summary": "...", "generated_at": "...", "source_fact_ids": [..],
"word_count": 1980, "word_budget": 2000, "provenance": [{"id": 12,
"origin_agent": "user", "source": "user", "tag": "direct"}, ...]}`
(`word_count`/`word_budget` added 2026-07-09, additive within contract 1.0;
clients may ignore them. The budget is enforced at generation by a rewrite
pass, never truncation.)

`provenance` (additive) is one entry per fact that fed the current
summary (same set as `source_fact_ids`), carrying that fact's **raw**
`origin_agent` and `source` columns plus a mechanically-derived `tag`; this
is how a client checks per-claim origin *without* trusting unlabelled prose
for attribution. `tag` is one of:

- `direct` (`origin_agent == "user"`): the owner saved it themselves.
- `mined` (`source == "chat"`): the miner distilled it from a multi-turn
  conversation. **No single turn or speaker is recorded**, so the summary
  prose must never attribute a mined claim to "the user" or any named
  participant.
- anything else: the raw `origin_agent` verbatim, such as a participant's
  own slug or an approved `mcp:<client>` write.

That remainder is deliberately not flattened into an invented category like
"curated", which would claim more certainty about authorship than the record
supports. The prose itself is instructed
the same way (mention provenance only when material, never invent a speaker
for a `mined` or unrecognised-tag entry) but `provenance` is the
mechanically-checkable source of truth; read it instead of parsing the
summary text for attribution.
Empty on a summary generated before this shipped (older `summary_sources` rows
simply have no `provenance` key; the field defaults to `[]`).
`POST /summary/regenerate`: async rebuild of the live profile. Not gated,
so any local process can trigger it (see "Open on loopback, and what that
means").

## Maintenance

`POST /consolidate`: advisory sweep, today covering exact-duplicate groups and
permanence ("pin") nominations. Async. It **writes nothing at all**: the sweep
reads the ledger and returns proposals, including for exact duplicates, and
applying any of them is a separate human action from the admin page. **Requires
the owner credential, even on loopback.**
`GET /jobs/{id}` → `{"kind": "distill|summary|consolidate|viz-embeddings",
"status": "running|ok|failed", "error": null, "result": null}`
**Requires the owner credential, even on loopback**, so a caller without one
can start an open async operation but cannot poll its result.
`result` is the only place an async operation's output lands, and it stays
`null` until `status` is `ok`; its shape depends on `kind`. A distill
returns mining counts: `{"added", "quarantined"}` always, plus up to three
only-when-nonzero keys:

- `deduped`: a re-mine was collapsed.
- `refused_supersede`: a proposal was refused because the new fact's event
  date is more than a day older than its target's. Old claims file as dated
  history and never retire newer truth.
- `deferred_supersede`: a held-for-review fact proposed a replacement.
  Quarantine cannot alter canon, so the proposal waits for human review.

Or, if another
distill of the same conversation was already in flight, `{"added": 0,
"quarantined": 0, "skipped_locked": true}` and nothing was mined. Read
the extra keys with a default rather than indexing them; on the ordinary
path none are there.
A consolidate returns `{"proposals", "clusters_scanned"}`, a summary
regenerate `{"summary_chars": n}`.
`error` holds the failure message when `status` is `failed` (e.g.
a missing API key), never a silent failure. Jobs live in the service's memory
only, so a restart forgets them and a later poll is a 404; read the result
before restarting, or just re-run the operation.
`POST /backup` → snapshot now; `GET /health` reports backup state. The service
also snapshots automatically: at startup and every `backup_interval_hours`
(default 6, env `MEMORY_BACKUP_INTERVAL_HOURS`, `0` disables the timer) while
running, skipping intervals with no DB change; `MEMORY_MIRROR_DIR` copies every
snapshot to a second folder.

## Admin & visualisation surface (NOT part of contract v1)

Serves the human's admin pages; may change without a contract bump. Privacy
invariant (test-enforced, and scoped to the `/v1/viz/*` endpoints): the
visualisation endpoints never return **fact content**. What they return is
geometry (ids, ages, importances, scores, coordinates). That is what makes
the Mathematics page safe to show.

The one thing that is not geometry: `GET /v1/viz/recalls` returns the `query`
text of past lookups, which is the owner's own words, because the live view
exists to show what was asked. So "no fact content" holds across `/viz/*`,
but "nothing readable" does not, and the page is not safe to screenshot
without first checking what is in that feed.

The invariant is a claim about the viz endpoints alone. The attachment and
summary-version endpoints in this same section deliberately DO return
content, because showing you your own files and profile text is their whole
job. Note the difference in who may ask: the attachment routes require the
owner credential, the summary-version routes do not (see "Open on loopback, and what
that means").

`GET /` (admin page) · `GET /math` (the Mathematics page).
All four attachment routes below require the owner credential, on loopback
too, like the exact-row fact routes (2026-07-25). They return fact content,
message bodies and document text, so a loopback-only rule was never enough.

### Attachments

`GET /v1/attachments`: every stored file with its conversation context
(`limit` defaults to 200, maximum 1000).
`GET /v1/attachments/{id}/file`: download the original bytes (`?inline=1`
renders in-browser for previews).
`GET /v1/attachments/{id}/preview`: the file in context: text excerpt (or
image/binary kind), the message it arrived with, and the ledger facts mined
from that conversation.
`DELETE /v1/attachments/{id}`: the attachments twin of the facts eraser:
human-initiated via the danger zone, the only delete path; content-addressed
bytes are unlinked only when no other row references them. Journals to
`erasures` like every eraser.

### Messages

`GET /v1/messages/resolve?source_app=&conversation=&message=`: maps a
producer's ref (how crossband names a message: source app, conversation
external id, message external id) to the internal row id, with a verbatim
preview and the erase's blast radius (live facts that would move to review,
attached files that would stay). Exists so a producer's erase link can land
on the admin page prefilled; the admin page reads
`#erase=<source_app>/<conversation>/<message>` and calls this. **Requires
the owner admin token, even on loopback.**

`DELETE /v1/messages/{id}`: the messages twin of the facts eraser (#45),
closing the crossband#106 loop - a voice turn discarded at its source may
already be ingested here, and no automated path may touch the copy; this is
the human hand. One row, no bulk form, never called by any producer's code.
The row leaves the archive and the search index; live facts mined from it
quarantine with a `source-deleted:` reason and surface in review (the owner
decides each - derived knowledge never silently vanishes); attachments that
rode the message are counted in the response, never cascaded. Journals to
`erasures`. **Requires the owner admin token, even on loopback.**

### Person records (#33, the fleet's identity home)

Apps that capture voices create person records here and upload their
accepted clips, so a learned voice survives a lost client data directory.
Membro never does voice identification itself; it records what apps
assert. All five routes **require the owner admin token, even on
loopback**:

`GET /v1/persons?since=<time>`: person records changed since then,
forgotten marks included - a syncing app deletes its local copies of
anyone marked forgotten. Each record carries slug, display name (and
whether the owner set it - an owner-set name survives client updates),
relationship, aliases, clip count, and timestamps.

`POST /v1/persons`: create or update by slug. Aliases combine; an alias
already belonging to a different person is refused (409), never
reassigned. A name membro has seen as a MODEL speaker label is refused
outright - the crossband participant boundary (#65), backstopped
server-side. Existing `guest:<alias>` facts link to the person on upsert
(the response reports how many).

`POST /v1/persons/{slug}/anchors`: upload one clip (base64). Content-
addressed - the same bytes for the same person is a no-op. Files live
under `voice_anchors/`, owner-only modes, every clip kept (owner
decision: no server-side pruning).

`GET /v1/persons/{slug}/anchors` and `.../{id}/file`: list and download,
for rebuilding a lost client cache.

`PATCH /v1/persons/{slug}`: the owner's rename (sets the owner flag, so
no client upsert changes the name again) and relationship. Never creates.

`POST /v1/persons/{slug}/anchors/{id}/move` (body `{"to": slug}`): a human
correction - this recording belongs to someone else. Bytes stay,
attribution changes; moving bytes the target already holds collapses to a
delete of the mis-attributed row. Crossband replays its local moves
through this, so a rebuild can never resurrect a corrected clip.

`DELETE /v1/persons/{slug}/anchors/{id}`: delete one clip - journalled in
`erasures`, bytes unlinked when no other row shares them. Crossband
replays its local clip deletes through this.

`POST /v1/persons/{slug}/merge` (body `{"into": slug}`): fold one person
into another - aliases, clips and fact links re-point; the losing row
stays, marked `merged_into`. Refused (410) when either side is forgotten.

`POST /v1/persons/{slug}/forget`: the one-press forget. Deletes the
audio from disk (one content-free `erasures` row), marks the person
forgotten, and moves their approved facts back into review as one
person-forgotten group (owner decision: nothing silently deleted).
Anchor routes answer `410 gone` afterwards; the record itself stays
listed so syncing apps learn to delete their copies.
The three summary-version routes below, unlike the attachment routes above,
are **NOT** gated: they answer an unauthenticated loopback caller.

### Summary versions

`GET /v1/summary/versions`: every generated profile, newest first (metadata
only; append-only history, so regeneration never destroys a version).
`GET /v1/summary/versions/{id}`: one version with its full text. Open on
loopback, so any local process can read any stored profile in full.
`POST /v1/summary/versions/{id}/restore`: make that version current again by
APPENDING a new version row (`restored_from` set); history is never rewritten.
Open on loopback, so any local process can change which profile is live.

### Visualisation routes

`GET /v1/viz/decay`: every live card's (age, importance, score) + formula constants.
`GET /v1/viz/embeddings`: cached 3D PCA of up to the newest 2,500 embedded
cards (`SAMPLE_CAP` in `viz.py`; the projection's Gram matrix costs O(n²)
memory, so past that point the newest cards win) with lifecycle timestamps
(superseded included) + current summary membership; `{"status": "computing"}`
while the background projection job runs. Both this endpoint and
`/v1/viz/landscape` return `sampled: true` when that cap actually bit, so the
page can say "showing the newest 2,500" instead of implying it drew
everything.
`GET /v1/viz/landscape`: the data for **"The life of your memory"**, one
always-current 3D scene (added 2026-07-18; 3D biome 2026-07-19; consolidated to a
single scene 2026-07-20; all additive). **Self-sufficient**: returns
`{"status": "computing"}` and kicks off the one-time PCA projection build itself
when cold (no separate `/embeddings` call needed), then serves the scene. Geometry
only, showing **alive** facts (non-superseded, non-quarantined) as they are now:

- `nodes`: id, **3D** coords `x,y,z`, importance, freshness score, cluster
  index.
- `clouds`: biomes from deterministic **k-means on the 3D coords**, bounded
  count ~sqrt(n/2) clamped to [6,14]. Each carries a 3D centroid `cx,cy,cz`,
  the 6 unique covariance entries `cov=[xx,yy,zz,xy,xz,yz]`, a `spread`
  radius, mean `freshness`, density `size`, and a palette index. The
  covariance entries let the client render a translucent volumetric
  ellipsoid via Σ₂=JΣJᵀ rather than a flat hull. The palette index only
  distinguishes a biome from its neighbours; it is never a fixed
  topic→colour map.
`edges` (co-occurrence: facts recalled together in the access log, weighted),
`sediment` (per current fact, the ids/dates/importances of the facts it
superseded), `summary_ids` (current summary membership, for the dot ring),
`sampled` (true when the 2,500-card projection cap bit; see
`/v1/viz/embeddings` above), and `notes` naming any layer the current ledger
can't yet fill. The admin UI now drives "The life of your memory" entirely
from this endpoint; `/v1/viz/embeddings` above is retained but no longer the
UI's source.

### Recall trace and the access log

`POST /v1/viz/recall_trace`: `{"query", "limit"}` → the recall pipeline,
instrumented. Per-card score components and fate (kept / collapsed / over /
dim), plus lifecycle and dup edges, so the client can replay the answer as of
any past time.
Kept in lockstep with `/v1/recall` by test. `limit` is silently clamped to 20
because the endpoint exists to draw a diagram rather than to export data.
`GET /v1/viz/recalls?after=<ts>`: lookup events from the persistent access log:
recalls, history searches, and per-round summary fetches, feeding the live view.
**Not gated**, and `query` is the caller's own question verbatim (first 200
characters), so this is the one `/viz/*` route that hands readable text to an
unauthenticated loopback caller.
Each event is `{ts, kind, origin, query}`: `kind` is `recall | search | summary`;
`origin` is `http`, `auto` (an ambient recall a client fired on the user's
behalf; `POST /recall` accepts an additive `origin` field, added 2026-07-11),
or `mcp:<client-name>` (MCP adapter processes write the same log, so external
tools' lookups appear too). The `access_log` table is
append-only like the ledger: each row records when, what was asked, and which
facts came back (ids + scores); the service never updates or deletes a row.

## MCP adapter (model-facing subset ONLY)

Tools: `recall_memory`, `save_memory`, `search_history`, `memory_summary`:
**in-process library calls against the same SQLite file, never HTTP**.
`memory_service/mcp_server.py` imports `recall`, `ledger`, `episodic` and
`summary` directly and opens `data/memory.db` itself; the semantics match
`/recall`, `POST /facts`, `/search` and `/summary` above, but no request is
ever made to the service. What that means in practice:
- The adapter needs read/write filesystem access to `data/`, and
  `MEMORY_DATA_DIR` must resolve to the same directory the running service
  uses; point it elsewhere and saves land in a different ledger that never
  appears in the admin UI.
- It keeps working while the HTTP service is stopped. (The database must
  already exist: the adapter connects, it does not build or migrate the
  ledger schema, which is the service's startup job. One deliberate
  exception: if the `access_log` table is missing, the adapter creates it
  itself on first write, so lookups against a not-yet-migrated database
  are still recorded rather than lost.)
- Its calls never traverse the API's loopback / bearer-token checks, so no
  `MEMORY_AUTH_TOKEN` is involved; filesystem permissions on `data/` are
  what governs access.

Every save carries `origin_agent = "mcp:<client-name>"` and is therefore
auto-quarantined: the write gate is unaffected by the missing HTTP hop
because it lives in `ledger.add_fact` rather than in the API layer. Admin
operations (approve / dismiss / delete / ingest / consolidate) are
deliberately NOT exposed over MCP, so external tools can propose facts but
can never approve, delete or otherwise alter canon.

`memory_summary` prepends a freshness header to the profile prose: a
`generated_at` stamp (UTC) plus a one-line reminder that time-sensitive or
active-thread status should be verified with `recall_memory`. This is additive
text, not a new wire field: a stale summary reads as authoritative, so the
consuming model must be able to see the age without a second call.

## Admin MCP adapter (read-only, opt-in, separate server)

`memory_service/mcp_admin_server.py` is a **second** MCP server, not part of the
four tools above and not registered by default. It exists for a session doing
ledger *remediation*, such as confirming a suspected mis-mined fact, which
needs exact rows rather than semantic recall. Two tools:
`search_facts(query, status)` and `review_queue(query)`, thin GET-only
wrappers over `GET /v1/facts` and `GET /v1/review` above. As of 1.1 both are
genuinely token-gated server-side (see "Owner admin token"), so this
wrapper's token requirement is enforced by the API rather than being a
client-side convention. Differences from the four model-facing
tools:
- Requires `Authorization: Bearer <MEMORY_AUTH_TOKEN>` matching the running
  service's own admin token, even against loopback (unlike the base API's
  loopback-is-trusted default for the open routes), because these two routes
  return exact ids and moderation state, which is more revealing than
  recall's paraphrased output.
- No `save`/write tool exists in this server at all: approve, dismiss, edit, and
  delete stay exactly where they were (human-only, via the admin UI/HTTP API).
- Talks HTTP to the running service (`MEMORY_API_URL`, its own process and
  connection); it never opens the sqlite file directly, so it works the same
  whether the calling session shares a filesystem with the service or is
  fully sandboxed from it.

## Invariants (behavioural contract, tested)

1. Append-only: no automated path deletes a fact; supersede/quarantine/dismiss only.
2. Episodic record is ground truth: never modified by any maintenance pass.
3. Quarantined facts never appear in `/recall` or `/summary`.
4. Untrusted-origin writes are always quarantined at creation.
5. Every fact carries `event_date` (never null) and `origin_agent`.
6. `/summary` claims trace to `source_fact_ids`, and each of those facts'
   raw `origin_agent`/`source` is exposed via `provenance`; the summary
   prose never asserts a speaker for a fact whose origin does not
   mechanically support one.

## Development

`GET /v1/disposable-identity` supports disposable benchmark stores; it
reports `disposable: false` on a real store and returns no token.
