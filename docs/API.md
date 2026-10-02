# Memory service API: the HTTP contract, v1.8

Membro's HTTP contract is what its client apps build against, and this
repo owns it. It's versioned, and a breaking change bumps the major
version. A client checks `contract_version` when it first connects, and
refuses a different major version. The same contract tests run in this
repo's CI, against the real service, and in each client's CI, against a
stub.

## Contract rules

- The base URL is `http://127.0.0.1:8901/v1`.
- Membro answers on loopback, the computer's own address. It refuses to
  bind any other address unless `MEMORY_AUTH_TOKEN` is set, and off
  loopback a request then needs `Authorization: Bearer <token>`. A host
  named in `MEMORY_TRUSTED_HOSTS` also lets through the sign-in routes,
  and any route for a browser with a live session. Anything else off
  loopback gets a 403.
- Every body is JSON. An error the service raises itself uses one
  envelope, `{"error": {"code": "...", "message": "..."}}`, with the
  usual HTTP status.
- Three kinds of error come straight from the web framework, and keep
  its own `{"detail": ...}` shape. A request that fails schema
  validation is a 422, with `detail` as a list by field. An unknown
  route is a 404 `{"detail": "Not Found"}`, and the wrong method on a
  known route is a 405. A client that parses error
  bodies should read `error` first and fall back to `detail`. One
  endpoint can return both shapes. `POST /facts` with no `content` key
  at all is a `detail` 422, and with 3 characters of `content` it's an
  `error` 422.
- An async operation returns `202 {"job_id": "..."}`. Poll
  `GET /jobs/{id}` for the result, which needs the owner credential, as
  [Maintenance](#maintenance) explains.

### What each minor version added

- **1.8: a capture app says which voice clips it keeps.** Person records
  count the stored clips its manifest leaves out, the owner's press
  deletes them, and a clip delete may say why. [Person
  records](#person-records) has the routes. With no manifest, it behaves
  as 1.7 did and counts nothing.
- **1.7: a save on `POST /facts` may name the caller's conversation.**
  `conversation_id` beside `source_app` is the same pair `/ingest` and
  `/recall` use. A save that carries `guest_speakers` is bound to that
  conversation when it's created, like a fact the miner ties to a
  guest's turn, and it's still held for review. When the service hasn't
  ingested that conversation yet, the save creates its record, and the
  first ingest fills the same record. The owner's own saves stay global.
  Without the field it behaves as 1.6 did, and a save made with guests in
  the room is held but stays global.
- **1.6: every fact carries a `scope`, and `/recall` may name the
  caller's conversation.** A `global` fact is recalled from anywhere. A
  `conversation` fact is recalled only from the conversation it came
  from, and never goes into the summary. A fact the miner ties to a
  guest's turn is bound when it's created. In 1.6 `POST /facts` names no
  conversation, so a save made with guests in the room is held for
  review and stays global. The owner's own facts are global. Of the
  facts stored before 1.6, the ones that came from a guest's turn were
  bound to their conversation once, on upgrade, and the rest stayed
  global. `POST /recall` accepts `source_app` and `conversation_id`, the
  pair the caller ingests under, and answers with the bound facts beside
  the global ones. Without the pair it answers with global facts only.
  Recall rows carry `scope`. `POST /facts/{id}/scope`, with the owner
  credential, widens or rebinds one fact. Without the new fields it
  behaves as 1.5 did, except that a fact from a guest is never recalled
  outside its conversation.
### Earlier minor versions

- **1.5: a save on `POST /facts` may carry `guest_speakers`.** These are
  the guests in the room when a model saved the fact, as the speaker
  values `/ingest` already uses, `guest:<name>` or `guest:unknown`. The
  field is a list of strings, at most 12. A save that carries it is
  quarantined with a `guest-present:` reason naming the guests. The
  mined path already holds a guest's words, because each message names
  its speaker, and this closes the same wall over a direct save. The
  origin gate outranks this stamp. A save that carries both stamps keeps
  its `web-derived:` reason with the guest clause added after it.
  Without the field it behaves as 1.4 did.
- **1.4: four added fields for a client that reads memory back.**
  `/search` hits carry `web_sources`. `/health` carries
  `browser_origin`. `GET /conversations/{app}/{id}/watermark` reports
  the highest message id held for one conversation. `event_date` is a
  calendar day at the owner's local midnight, whichever writer set it,
  the MCP `save_memory` tool included. A 1.3 client sees nothing new,
  and keeps working.
- **1.3: a message on `/ingest`, and a save on `POST /facts`, may carry
  `web_sources`.** These are the web domains a tool read in the round
  that produced the message or save, as a list of strings, at most 20.
  They're stored word for word beside the message. A fact born from a
  stamped message, or saved with a stamp, is quarantined with a
  `web-derived:` reason naming the domains. A public page mustn't be
  able to write memory by phrasing a sentence well. The origin gate
  outranks the stamp. Without the field it behaves as 1.2 did.
- **1.2: a message on `/ingest` may carry `speaker_identity`.** It's the
  person record the sending app believes spoke, with a `person` name, a
  `confidence` from 0 to 1, and a `method`. The method is one of
  `introduced`, `voice-match`, `by-elimination` and `owner-correction`.
  It's stored word for word beside the message. A fact born from an
  identified message links to that person when the identity is strong.
  That's always for `introduced` and `owner-correction`, for
  `voice-match` at 0.8 or more, and never for anything weaker. Without
  the field it behaves as 1.1 did.

### Always gated, even on loopback

Some routes need the owner credential every time, even on loopback,
and have since 1.1. That's a separate, stricter check from the loopback rule in
[Contract rules](#contract-rules). These `/v1` routes carry it, and the
list is complete:

- `GET /facts`, `GET /review`, and every verb on one existing fact by
  id, which are `PATCH /facts/{id}`, `/facts/{id}/supersede`,
  `/facts/{id}/approve`, `/facts/{id}/dismiss`, `/facts/{id}/scope` and
  `DELETE /facts/{id}`
- the bulk ledger verbs, `POST /facts/quarantine`,
  `POST /facts/bulk-approve`, `POST /facts/bulk-dismiss` and
  `POST /review/dismiss-all`
- `POST /search`, `POST /consolidate` and `GET /jobs/{id}`
- all four attachment routes, `GET /attachments`,
  `GET /attachments/{id}/file`, `GET /attachments/{id}/preview` and
  `DELETE /attachments/{id}`
- both message routes, `GET /messages/resolve` and
  `DELETE /messages/{id}`
- every person route under `/persons`, and `DELETE /unused-clips`

No other `/v1` route carries it. Outside `/v1`, the passkey enrolment
and management routes need the same credential, and so does
`GET /app-links`, the admin page's row of links to your other apps.
`GET /` isn't on the list, and still varies by credential. A caller with
no credential gets the locked page in place of the admin page, and no
401.

### Open routes

Every other `/v1` route answers a caller on loopback with no credential,
and only the loopback rule governs it:

- `/health`, `/busy`, `/disposable-identity` and `/backup`
- the ingest watermark,
  `GET /conversations/{source_app}/{conversation_id}/watermark`
- `/ingest`, `/distill`, and `POST /facts` to create a fact
- `/recall` and `GET /summary`
- `POST /summary/regenerate`, which rebuilds the live profile
- all three `/summary/versions*` routes, including the one that returns
  a stored profile in full and the one that restores it over the live
  profile
- every `/viz/*` route

The profile routes and `/viz/recalls` are the ones you'd most likely
expect to be gated. [Open on loopback](#open-on-loopback) says what that
means.

## Handshake

`GET /health` returns this.

```json
{
  "status": "ok|degraded",
  "contract_version": "1.8",
  "browser_origin": "http://127.0.0.1:8901",
  "db": {"facts": 0, "messages": 0, "size_bytes": 0, "integrity": "ok",
          "fts_in_sync": true, "last_backup_at": null},
  "capabilities": {"embeddings": true, "miner_model": "claude-haiku-4-5"},
  "detail": {"…admin surface, may change without a contract bump…"}
}
```

The chat client calls it when it starts. If Membro answers and the
versions match, the client turns its memory features on, and otherwise
it runs without memory.

`status` is `"degraded"` whenever SQLite's integrity check,
`PRAGMA quick_check`, fails. That's the point of a health check, because
a client can then decline to write into a damaged file. It's also
`"degraded"` when `fts_in_sync` is false. That flag goes false when
either search index, for messages or for files, is out of step with its
table, or was built with a tokenizer other than the current one. An
index that was dropped and made again starts empty, and never refills
on its own. The service finds and repairs this when it starts, and
`scripts/rebuild_fts.py` does the same for a running instance without a
restart. While the message index is empty, search falls back to slower
substring matching. An index that's only partly behind returns too few
rows, with no error. A client that sees `fts_in_sync: false` should treat
search results as unreliable until it flips back, and never read them as
an empty archive.

`browser_origin`, added in 1.4, is the address a browser can open the
service at. It's the `browser_origin` setting when the operator set one. With that
unset, it's `https://<first trusted host>:<tailscale_port>` when a
browser may sign in from a tailnet name, and loopback when not. A
client that links someone to the admin page, such as the message
eraser, uses this and never guesses a host and port.

`status`, `contract_version`, `browser_origin`, `db` and `capabilities`
are part of the contract. `detail` isn't. It carries the whole internal
health record for the admin page's health panel, and may change without
a contract bump. It holds the SQLite version, the journal mode,
integrity, size, a count of facts that are current, superseded and
quarantined, counts of messages and conversations, a block on
attachments, the last backup, and the backups kept. It also carries
`dropped_by_reason`, a count kept in memory of facts the walls dropped
since the process started, like `{"system-meta": 3,
"builder-process": 11}`. That count resets on restart, and it never
records what was dropped. Clients should read `db`, never `detail`.

## The owner credential

Every route in [Always gated](#always-gated-even-on-loopback) needs a
valid credential, even from 127.0.0.1. Search returns transcript
excerpts word for word, and attachments return file bytes and document
text. The message routes preview and erase one archived message, the
person routes return names and voice clips, and job rows carry the
results of operations. They're all gated for the same reason as the
fact routes.

Either credential passes the check. One is `Authorization: Bearer
<token>`, the real admin token, which the admin MCP server and scripts
use. The other is the `mm_admin` session cookie a browser gets from
`POST /login`, which holds an opaque session id and never the token. A
coding agent running in a sandbox on the same computer shares its
loopback address. Without this check, it could list exact fact ids,
text and status, read the review queue, and search the raw transcripts
with no credential at all. That would defeat the point of making the
admin MCP server opt-in.

### The rules the tests hold

- **A page shown before sign-in never holds a credential.** `GET /`
  serves the real admin page only to a caller already signed in, with a
  valid `Authorization` header or the session cookie. Anyone else gets a
  locked page with the sign-in forms, and no ledger data or secret
  anywhere in the response. When a password exists, the page has the
  password form and a "Forgot your password?" reset form. When a
  passkey exists for the address the page is open on, it also leads
  with a passkey button. On a first run it asks for a recovery secret
  and a new password, as [Owner password login](#owner-password-login)
  describes. The admin token is refused as the password. It's taken
  only as the recovery secret, on the enrol and reset forms.
- **A session is an opaque id the server keeps.** `POST /login` makes a
  fresh, random session id with `secrets.token_urlsafe(32)`, and records
  its expiry on the server, in the `sessions` table. The id is never
  made from, or equal to, anything the client sent, so nobody can plant
  one in advance. The table holds only a SHA-256 hash of the id, so a
  restart signs nobody out, and a copy of the database can't sign
  anyone in. It sits beside the password verifier, as sign-in state
  outside the ledger. The cookie carries only that id.
- **A copy of the cookie is limited, and can be revoked.** It expires
  after 24 hours. `POST /logout` deletes the session on the server, and
  that ends every copy of the cookie at once, beyond the one in the
  browser that clicked "Log out". The real admin token is used only for
  `Authorization: Bearer`, by the admin MCP server and scripts, and for
  the recovery check on enrol and reset. It never goes in a cookie. A
leaked session id can't reveal the token, and revoking a session never
touches the token or any other session.

### Getting the token

You always get the token outside HTTP, never over it.

- If you've configured one, that's the token, and it stays the same
  across restarts, which a registered MCP client needs. Set it as
  `MEMORY_AUTH_TOKEN` in `.env` or the environment, or as `auth_token`
  in `config.local.json`. `start.sh` loads `.env` before it starts the
  service. The owner already knows it, because they set it.
- With none configured, the server makes a fresh random token for the
  life of its process. It prints it at startup only on a first run, before a
  password is enrolled, and never in an HTTP response. Under launchd the
  output goes to `data/service.log`, so a printed token stays in that
  file. After enrolment the startup line says the token isn't shown.
  Set `MEMORY_AUTH_TOKEN` and restart when you need one for a reset, an
  MCP server or `curl`.
- A configured token is never printed, because it's already in `.env`.

### Owner password login

The everyday browser sign-in is a lasting password, so an owner never
has to hunt for a token in the terminal after a restart. The admin token
keeps two jobs. It's still the `Authorization: Bearer` credential for
MCP and `curl`, and it's the recovery secret, from outside HTTP, that
gates enrolment and resets. It isn't accepted as the everyday sign-in.

- The password is stored only as a memory-hard scrypt verifier, with
  its salt, settings and derived hash. The password itself is never
  stored, and nothing can be reversed. The verifier lives in the lasting
  `settings` table, so it survives restarts. An older database with no
  verifier yet goes into first-run enrolment, and needs no change to its
  schema.
- `POST /login` takes the form field `password=<value>` and checks it
  against the verifier. A correct password makes an opaque session id,
  sets `mm_admin`, and redirects to `/`. The cookie is `HttpOnly` and
  `SameSite=Strict`, holds the id and never the token, expires, and
  lasts across restarts. A wrong password, or the admin token typed
  here, gets the locked page again, and no session. Before enrolment
  there's nothing to check against, so sign-in fails, and the page
  offers enrolment.
- `POST /enroll` takes `recovery=<admin token>`, `password` and
  `confirm`, on a first run only. It's a 409 once a password exists.
  The recovery secret stops a caller with no credential on loopback,
  like a coding agent in a sandbox, from enrolling itself, because it
  never sees the secret in the terminal or `.env`. A wrong recovery
  secret is a 401, and a missing one is a 422, and nothing is written
  either way. The two passwords must match, and be at least 8
  characters. On success the verifier is saved, and the browser is
  signed straight in.
- `POST /reset` takes `recovery=<admin token>`, `password` and
  `confirm`. It's the same proof, allowed at any time, and it replaces
  the verifier. Every existing session ends, and the browser that reset
  gets a fresh one.
- `POST /logout` revokes the session on the server, and clears the
  cookie.

### Passkey login

Once a passkey is enrolled, the lock screen offers it first, and the
password moves one click behind it. A successful passkey sign-in makes
the same opaque session as a password sign-in. The password and the
recovery secret don't change. A passkey is tied to the web address it was
made on. `localhost` and each trusted host enrol separately, and an IP
address like `127.0.0.1` can never hold one.

- `POST /webauthn/register/options` and `POST /webauthn/register` start
  and finish enrolment for the address the page is open on. Both need a
  signed-in session or the bearer token. Only the public half is stored,
  which is the credential id, public key, address and counters, beside
  the password verifier in the `settings` table. The private key never
  leaves the authenticator.
- `POST /webauthn/login/options` and `POST /webauthn/login` belong to
  the lock screen. A challenge goes out, a signed assertion comes back,
  and it's checked against the enrolled public key, with user
  verification like Touch ID or Face ID required. The options list the
  ids of this app's passkeys for this address, so the browser offers
  only Membro's own. Credentials are still enrolled as discoverable.
- `GET /webauthn/credentials` and `DELETE /webauthn/credentials/{id}`
  list and remove enrolled passkeys, with a signed-in session or the
  bearer token. Removing one can never lock the owner out, because the
  password always remains. It ends every other session, so a lost
  phone's session goes with its passkey. The browser doing the removal
  gets a fresh session.

These routes belong to the admin page in the browser. They're hidden
from the schema with `include_in_schema=False`, and sit outside the
versioned `/v1` contract, so `contract_version` doesn't change for them.

### Setting up the admin MCP server

These steps set up `memory_service.mcp_admin_server`.

1. Configure a stable token. Put `MEMORY_AUTH_TOKEN=<random value>` in
   `.env`, or `"auth_token"` in `config.local.json`.
2. Restart the service, so it picks up that value as
   `app.state.admin_token`. A process that's already running keeps the
   token it had when it started, configured or made up. The same restart
   makes the value work as the recovery secret on the admin page.
3. Register the MCP server with the same token and the service's
   address. Pass both with `-e`, so they reach the server when it runs
   later. A shell variable in front of `claude mcp add` sets them only
   for the registration command, and the registered server then starts
   without them.

   ```sh
   claude mcp add -s user membro-admin \
     -e MEMORY_AUTH_TOKEN=<token> \
     -e MEMORY_API_URL=http://127.0.0.1:8901/v1 \
     -e PYTHONPATH=<repo> -- \
     <repo>/.venv/bin/python -m memory_service.mcp_admin_server
   ```
4. Check it with one harmless read. `search_facts("")` or
   `review_queue()` should return real rows, or a line starting "No
   matching", and never an error about credentials.

A correction from the owner always outranks model-mined history that
contradicts it. When `search_facts` or `review_queue` turns up older
mined facts that contradict something the owner has since said
directly, the newer correction is the authority. These tools show where
a fact came from, `origin_agent`, and its status, so a session cleaning
up the ledger can propose a supersession. Repeated or detailed mined
facts are never, on their own, a reason to prefer them over the owner's
word.

### Open on loopback

Some routes outside the gated set reveal more than you might expect.
[SECURITY.md](../SECURITY.md) says which they are and what that means.
Each route's own section here says what it does and who may call it.

## Episodic record

`POST /ingest` adds transcript messages. Sending the same message again
does nothing, because a message is unique by its conversation and its
`external_id`.

```json
{"source_app": "multi-model-chat", "conversation_id": "chat-123",
 "title": "Weekend plans",
 "messages": [{"external_id": "m-1",
                "speaker": "user|<slug>|guest:<name>|guest:unknown",
                "content": "...", "created_at": "2026-07-04T10:00:00+10:00",
                "attachments": [{"filename": "notes.txt", "mime": "text/plain",
                                  "data_b64": "..."}]}]}
```

It returns `{"ingested": 12, "skipped": 3, "attached": 1}`.

`title` is the conversation's name for people, and it's optional. It's
applied again whenever a later ingest of the same conversation sends
one, so a chat renamed in the client catches up here. An empty or
missing title leaves the stored one as it is. It's the title `/search`
hits carry back, and the admin pages show. A client that never sends
one leaves every conversation blank.

### Guest speaker classes

Beside `user`, the owner, and a bare model slug like `claude`, a
message's `speaker` may be `guest:<name>`, for another named person in
the session, such as a voice session with several people in it. It may
also be `guest:unknown`, for a turn from a person whose voice couldn't
be told apart with confidence. `source_app` and how a conversation is
identified don't change. The classes matter in the mining pass.

- A fact the miner draws from a guest's speech is quarantined by
  default. It's written, then held in the review queue with the guest's
  name in the reason, the same treatment `mcp:*` writes get from the
  write gate. Speech from `guest:unknown` is always quarantined.
- Pronouns resolve for each speaker. A guest's "I" makes a fact about
  the guest, written into the owner's ledger in the third person, and
  never folded into the owner's own profile. Guests get no profile of
  their own. This service has one owner, and a guest's facts exist only
  as facts about the owner's world. A fact about the owner said by a
  guest still comes from the guest, and it's held the same way.
- It fails safe. Any other speaker value with a class prefix, say
  `agent:scribe`, isn't recognised and is treated as untrusted. It's
  held like guest speech, so a newer client can never make itself
  trusted by inventing a class. Facts drawn from the owner's own `user`
  turns don't change.
- A source turn is recorded only when it's real. A mined fact carries
  `source_message_id` only when it was tied to one turn. A fact the
  miner couldn't bind is stored unbound, with `source_message_id` null,
  and never pinned to whichever turn ended the mining window.
  `scripts/relink_unbound_facts.py` can bind one later, in a chat with
  no guest speech, to the owner's own turn that clearly shares its
  wording. In a window with a guest in it, such a fact is still held for
  review.
- When the miner supplies a missing binding on its corrective retry,
  the answer is checked against the turn it names. A turn that shares
  none of the fact's wording is refused, and so is one no more likely
  than a guest's turn in the same window, and the fact stays held. A
  retry's guess about who spoke can't turn a guest's sentence into the
  owner's trusted facts.

### Attachments on ingest

`attachments` is optional, on each message. Files are part of the
episodic record. Their bytes are stored whole, by their content, under
`data/attachments/`. Their text is pulled out into the full-text index,
so `/search` and mining see it. That covers `text/*` files and files
with common text extensions like `.txt`, `.md`, `.csv`, `.json`, `.yaml`,
`.html`, `.py`, `.js` and `.sql`, in full. A PDF, matched by its type or
its `.pdf` extension, is read with pypdf, as well as it can be. A hit
from a file carries `speaker: "file: <name>"`. An image has no text of
its own, so once a distill captions it, the caption is searched in its
place. Attachments attach to messages already ingested, the skipped ones,
too, so filling in old conversations is a plain ingest again. Like
messages, they're only ever added and never changed. The `attached`
count is new rows, so a repeat send counts 0.

One request takes at most 5,000 messages, and more is a 413. A message
takes at most 20 attachments, and more is a 422. A file can be about 25
MB once decoded, and bigger is a 422. These limit one request, and never
your history. Storage itself has no limit, so send a long backlog in
batches, and not as one giant request.

### Distill

`POST /distill` runs the mining pass, with the walls, over a
conversation's ingested content that hasn't been mined. It's async.

```json
{"source_app": "multi-model-chat", "conversation_id": "chat-123"}
```

An optional `regenerate_summary`, true by default, rebuilds the live
profile whenever the pass mines a new fact. A bulk run can turn it off
and rebuild once at the end. The route is open on loopback.

### Verbatim search

`POST /search` searches the episodic record word for word. Words match
on their stem, so "sister" finds "sisters", and "packed" finds
"packing". It needs the owner credential, as `Authorization: Bearer` or
the session cookie, even on loopback. Search returns transcript excerpts
word for word, which reveal at least as much as the exact rows of the
ledger.

```json
{"query": "...", "limit": 20, "origin": "http"}
```

It returns this.

```json
{"hits": [{"conversation_id": "...", "title": "...", "speaker": "...",
           "content": "...", "created_at": 1783123200.0,
           "web_sources": ["example.com"]}]}
```

`limit` defaults to 20, with a maximum of 500, and anything outside 1 to
500 is a 422. Messages come first, and files fill the rest of the limit.
`title` is the conversation's title as last ingested, or an empty string
if the client never sent one. `created_at` is in Unix seconds.
`content` is up to 64 words around the match, with `>>match<<` markers,
and never the whole message.

Search falls back to plain substring matching when the message index is
empty while messages exist, or when the index can't run the query. The
fallback matches the first word only, in messages only, newest first,
and cuts its excerpt the same way. A hit on an image's caption is cut
from the caption the same way too, and its speaker says what it is,
`file: <name> (image caption)`.

`web_sources`, added in 1.4, is the list stored with the message on
ingest. It's empty for a turn that read no web page, and for hits from
files. A client that shows a hit to a model should mark a stamped hit as
untrusted, the same way it marks a live fetch.

`origin` is only a label for the access log, the same as on `/recall`,
and it changes nothing about what comes back. Send `auto` for a search a
client ran on the user's behalf, and leave it at `http` for one a model
asked for. It's optional within 1.8. A 1.8 service that predates it
ignores it, so a client can send it without checking the version.

### Ingest watermark

`GET /conversations/{source_app}/{conversation_id}/watermark`, added in
1.4, is open on loopback like `/health`. It carries ids and a count, and
never content.

It returns `{"highest_external_id": "412", "messages": 87}`.

`highest_external_id` is the largest message `external_id` this service
holds for that conversation, or `null` when it holds none. Ids compare
as numbers when every id is a string of digits, and as text otherwise. A
message the owner erased still counts, because its id sits in the
erasure journal. A client that wound back past it would send again the
message that was just erased. An unknown conversation is a 404, in the
standard envelope. A client that keeps its own "ingested up to" mark
compares the two on each handoff, and winds its mark back when this
service has less. That's what a restore from a snapshot leaves behind.

## Ledger

`POST /facts` saves one fact.

```json
{"content": "...", "event_date": "2026-07-04", "confidence": "high|medium|low",
 "origin_agent": "user | <participant-slug> | mcp:<client>",
 "source_app": "<registered app>",
 "web_sources": ["<domain>", "..."],
 "guest_speakers": ["guest:<name>", "guest:unknown"],
 "conversation_id": "<the caller's own conversation id>"}
```

`content` has its whitespace collapsed first, and must then be 8 to
10,000 characters. Anything shorter or longer is a 422, with "nothing
meaningful to save" or "fact too long". `event_date`, since 1.4, is a
calendar day. Send `YYYY-MM-DD`, or a full timestamp, and the service
keeps only the day it falls on in the owner's local time. It's stored as
that day's local midnight, so two facts about one day compare equal, and
recall breaks the tie on the save time. Leave it out, and the fact is
dated to the day it was saved, so `event_date` is never null, which is
invariant 5. `source_app` is what the gate reads. `confidence` defaults
to `high`, and `origin_agent` to `user`.

### Web and guest stamps

`web_sources`, from 1.3, and `guest_speakers`, from 1.5, are optional
stamps, and both default to empty. `web_sources` lists the web domains
read in the round that produced the save, at most 20. They're lowercased,
and the hold reason names up to five of them, in alphabetical order. They
aren't stored on the fact. `guest_speakers` lists the guests in the room
when a model made the save, as `/ingest` speaker values, at most 12.
That's `guest:<name>` for one person besides the owner who was told
apart with confidence, or `guest:unknown` for a person who couldn't be.
Guest entries are stripped, empty ones dropped and repeats removed, in
their order. An entry that isn't a guest class, like a model slug,
`user` or an unknown prefix, is dropped, and never refused.

A save that carries either stamp is held for review. `web-derived:`
names the domains, and `guest-present:` names the guests in plain
English. `guest:unknown` reads as "an unidentified guest", and at most
five guests are named. When both stamps are there, the reason keeps the
`web-derived:` prefix, and the guest clause follows after `; `. The
origin gate outranks both stamps.

`conversation_id`, from 1.7, at most 64 characters, names the
conversation the save was made in. With `source_app`, it's the pair the
caller ingests under. A save with a guest stamp is bound to it, so once
it's approved it's recalled only there. A save with no guest stays
global, and records the conversation when the service holds it. A save
with guests present that arrives before the conversation's first ingest
creates the conversation's empty record, and that ingest fills it.
Without the pair, a save with guests present is held but stays global.
The response's `scope` says which happened.

### The write gate

The gate is invariant 4, and it applies to the write itself, whoever
wrote it. A write reaches the trusted facts only if `origin_agent` is
`user`, or it names a `source_app` in the trusted set. Anything else is
created quarantined, and the response says `"quarantined": true`. Its
reason starts "external write", with the origin. An `mcp:*` origin is
never trusted. Even beside a trusted `source_app` it stays held, so an
adapter, or any caller faking that origin over the local API, can't pass
a fact into the trusted set by naming a trusted app.

A write the origin gate holds has its `confidence` forced to `low`, so a
caller that sent `high` reads back `low`, and an unreviewed claim never
looks confident. A trusted write held only for a web or guest stamp
keeps the confidence it was sent with. The response includes
`{"id": 1, "quarantined": bool, "scope": "global" | "conversation"}`,
with `scope` since 1.6.

To stage a fact for the owner's review through the API, like an agent
proposing a change, post it with an `origin_agent` that isn't `user`,
such as the model's own slug, and no trusted `source_app`. It lands in
`GET /facts?status=quarantined`, for the owner to `approve` or
`dismiss`.

### Reading the ledger

`GET /facts?status=valid|superseded|quarantined|all&q=...&limit=...&before=...`
lists and filters facts, newest first. It needs the owner credential,
even on loopback. `status` defaults to `valid`. `q` matches content with
`LIKE`, so it ignores case for plain letters, and `%` and `_` in it act
as wildcards. `limit` defaults to 100, with a maximum of 1,000.

To read further back, pass `before` with the id of the last fact you
got, and only facts with a smaller id come back. Keep going until a
page comes back shorter than `limit`. A fact saved between two pages
doesn't shift the next one, so no row shows twice. `before` must be a
whole number from 1 up.

A `q` that starts with `#` followed by ids is a lookup by id, and not a
content search. That's one id, or several separated by commas or spaces.
The leading `#` is what tells them apart, because a bare number is a
fair thing to search the text for, like a year or a figure. Unknown ids
are left out of the result, and they're no error. `before` doesn't apply
to a lookup by id. `limit` never trims a list of ids, because an
explicit list asks for those rows, and trimming it would read as "those
ids don't exist". `status` still applies, so look up a held fact with
`status=all`.

`status` isn't checked on the server. Only `valid`, `superseded` and
`quarantined` filter anything, and any other value applies no filter
and returns every row. That's how `all` works. It also means a typo,
like `quarantied`, returns everything with no 422, so check the
spelling before you trust a count.

`PATCH /facts/{id}` edits `content`, `event_date` or `confidence`, and
embeds the fact again. It also edits `importance`, because the person
outranks the miner on how a fact ages. `importance` must be a whole
number from 1 to 10. Anything outside that is refused with a `detail`
422, and never clamped, so send a value in range. It needs the owner
credential, even on loopback.

### Changing a fact's state

Every route here needs the owner credential, even on loopback.

- `POST /facts/{id}/supersede` takes `{"successor_id": 2}`. It's about
  when a fact holds, and never deletes it.
- `POST /facts/{id}/approve` takes a fact out of quarantine, and only a
  person does it.
- `POST /facts/{id}/dismiss` marks a fact reviewed and kept out, and
  destroys nothing. Only a person does it.
- `POST /review/dismiss-all` is the bulk twin of
  `/facts/{id}/dismiss`. It dismisses every fact in the review queue in
  one call, with the same safe effect. Each fact stays quarantined and
  in the ledger, and `/approve` reverses any one of them. It returns
  `{"dismissed": <count>}`. It's for clearing a backlog that a fix to a
  filter made stale, and not for everyday triage.
- `POST /facts/quarantine` quarantines facts that were already accepted
  as trusted. It takes `{"ids": [1, 2], "reason": "..."}`, and `reason`
  is required and can't be empty, because nobody can judge a row in the
  review queue that has no reason. It returns
  `{"quarantined": [...], "skipped": [...]}`. Unknown ids and ones
  already quarantined are skipped, and they're no error, so running it
  again does nothing. The facts leave recall at once, and the summary
  five minutes after the last hold. They stay in the ledger, appear in
  `GET /review`, and `/facts/{id}/approve` reverses each one. It's the
  third treatment between `supersede`, which names a replacement a
  broken row doesn't have, and `DELETE`, which destroys.
- `DELETE /facts/{id}` is one of the three erasers, for facts,
  attachments and messages. Only a person starts it, never automation.
  Every erasure adds a row to the `erasures` journal that holds no
  content, only the kind, the references and when. When the live
  summary was built from the erased fact, it's rebuilt straight away.

### The review queue

`GET /review` returns the queue of facts held for review. It needs the
owner credential, even on loopback.

Each row is the whole fact row, plus `source`. That's the turn the fact
was mined from, so a reviewer's first question, who said this, is
answered without leaving the queue.

```json
"source": {"message_id": 812, "speaker": "guest:Sam", "speaker_class": "guest",
           "created_at": 1754616000.0, "excerpt": "I hate coriander…",
           "truncated": false}
```

`source` is `null` whenever the fact names no source message, or the
message it names has since been erased. That covers an external
`mcp:*` write, a fact saved by hand, and a mined fact that couldn't be
tied to one turn, as [Guest speaker classes](#guest-speaker-classes)
describes. It's never filled in with a nearby turn. "Sam said this" and
"nobody knows who said this" are different decisions, and a queue that
blurs them is worse than one that stays quiet. `excerpt` is the first
400 characters of the message's own content, without attachment text,
and `truncated` says whether there's more. `speaker_class` is the class
mining uses, which is `owner`, `model`, `guest`, `guest-unknown` or
`unrecognised`.

Each row also carries `reason_class`, a stable token taken from the
start of the hold reason. The common ones are `guest-attribution`,
`guest-present`, `speaker-trust`, `grounding`, `temporal`,
`source-trust`, `source-trust-judged`, `source-deleted`,
`person-forgotten`, `importance`, `web-derived` and `external-write`.
Any other lowercase `word:` prefix is returned as a class of its own,
and a reason with no prefix is `other`. A reason with several flags
takes the class of its first. The admin page groups the queue by it.

`POST /facts/bulk-approve` and `POST /facts/bulk-dismiss` take
`{"ids": [...]}`, with the owner credential. They act on that list of
ids, and touch only the ids in the queue now. Everything else is
skipped, with no error, so one decision clears a whole cause, and never
sweeps up rows the owner hasn't seen.

Each row also carries `scope` and `conversation`, since 1.6. For a fact
bound to one conversation, `conversation` is `{"id", "source_app",
"external_id", "title"}`, the chat it will be recalled from and nowhere
else. For a global fact it's `null`.

## Recall and summary

`POST /recall` ranks facts by meaning, matching words and a small boost
for newer facts, and collapses paraphrases. Quarantined facts are always
left out, and superseded ones too, unless you set
`include_superseded`. `limit` is capped at 50. Recall is a retrieval
aid, and an unlimited top N over an empty query would be a whole-ledger
export. The response carries only the fields shown here. This route
answers callers on loopback with no credential, so what it returns is a
security boundary. Adding a field is a change to the contract.

```json
{"query": "...", "limit": 10, "include_superseded": false, "origin": "http",
 "source_app": "multi-model-chat", "conversation_id": "42"}
```

It returns this.

```json
{"facts": [{"id": 1, "content": "...", "event_date": "...", "confidence": "...",
            "origin_agent": "...", "score": 0.87, "scope": "global"}]}
```

`source_app` and `conversation_id`, since 1.6, name the caller's own
conversation, the same pair it ingests under. Every fact carries a
`scope`. A `global` fact is recalled from anywhere, and a `conversation`
fact only from the conversation it came from. A fact the miner ties to a
guest's turn is bound to its conversation when it's created. A save on
`POST /facts` made while guests were in the room is bound the same way,
when it names its conversation, which 1.7 added. A save with guests present that names none
is held for review and stays global, and once it's approved it's
recalled in every chat. The owner's own facts are global. Of the facts
stored before 1.6, the ones that came from a guest's turn were bound to
their conversation once, on upgrade, and the rest stayed global.

A recall that names its conversation gets the facts bound to it beside
the global ones. A recall without the pair, or naming a conversation the
service hasn't ingested, gets global facts only. Bound facts never go
into the summary. `POST /facts/{id}/scope` with `{"scope": "global"}`,
and the owner credential, is the one way to widen a fact. Approving a
held fact keeps its scope.

`limit` defaults to 10, with a maximum of 50, and anything outside 1 to
50 is a 422. `origin` is only a label for the access log, and it changes
nothing about what comes back. It's `http` by default, or `auto` for a
recall a client fired on the user's behalf without a model asking, or
`mcp:<client>`. The live view on `/math` reads it to tell "prepared
context" from "went deep", as [Recall trace and the access
log](#recall-trace-and-the-access-log) describes.

An empty `query` skips scoring and returns the most recent facts that
aren't quarantined, newest first. That's a cheap way to ask "what do you
know about me lately". Those rows carry no `score` field at all.

### The recall projection

The response is the projection shown, and nothing more. That's `id`,
`content`, `event_date`, `confidence`, `origin_agent`, `score` and
`scope`, and `score` appears only on a scored recall. The set is
`RECALL_FIELDS` in `api.py`, and a test fails if a field is added
without changing the contract. The rest of the fact row doesn't travel
over this route. That's `created_at`, `importance`, `source`,
`conversation_id`, `source_message_id`, `content_hash`,
`invalidated_at`, `superseded_by`, `quarantined_at`,
`quarantine_reason` and `review_dismissed_at`.

Plan around one result of that. With `include_superseded: true` the
superseded facts come back, but no field marks them as superseded.
`invalidated_at` and `superseded_by` are both outside the projection, so
an HTTP client can't tell a retired fact from a current one. The MCP
adapter's recall never asks for superseded facts either. A caller that
needs to know where a fact stands must use the owner-gated
`GET /facts`.

### Summary

`GET /summary` returns this.

```json
{"summary": "...", "generated_at": 1783123200.0, "source_fact_ids": [12],
 "word_count": 1980, "word_budget": 2000,
 "provenance": [{"id": 12, "origin_agent": "user", "source": "user", "tag": "direct"}]}
```

`generated_at` is in Unix seconds, or `null` before the first build.
Clients may ignore `word_count` and `word_budget`. The budget is kept by
rewriting when the profile is built, and never by truncating. A draft
more than 20% over the budget gets one pass that compresses it. If that
pass fails, the long draft stays, so `word_count` can be over
`word_budget`. The builder aims for `memory_summary_fill` of the
budget, 0.8 by default, up to the budget, so `word_count / word_budget`
reads as the fill.

`provenance` has one entry for each fact that fed the current summary,
the same set as `source_fact_ids`. Each carries that fact's raw
`origin_agent` and `source` columns, and a `tag` worked out
mechanically. That's how a client checks where each claim came from,
without trusting unlabelled prose to name who said it. `tag` is one of
these.

- `direct`, when `origin_agent` is `user`. The owner saved it.
- `mined`, when `source` is `chat`. The miner drew it from a
  conversation. Many mined facts are tied to one turn, which
  `GET /review` shows, but the tag names no speaker. The summary's prose
  must never say a mined claim came from "the user" or any named
  person.
- anything else, which is the raw `origin_agent` word for word, such as
  a participant's own slug or an approved `mcp:<client>` write.

That remainder is kept as it is, and never folded into a made-up
category like "curated", which would claim more about who wrote it than
the record supports. The prose is told the same thing. It mentions
where a fact came from only when that matters, and never invents a
speaker for a `mined` entry or a tag it doesn't recognise. `provenance`
is the source of truth a machine can check, so read it, and don't parse
the summary's text for who said what. A summary stored without
provenance returns `[]`.

`POST /summary/regenerate` rebuilds the live profile, async. It isn't
gated, so any program on the computer can start it, as [Open on
loopback](#open-on-loopback) explains. It spends model calls, and the
profile it replaces is kept as a version. A rebuild the model doesn't
finish fails the job with the reason, and the live profile stays as it
was.

Membro also rebuilds the profile by itself when it was built from a
fact that's since been held or erased. An erase of a fact or a message,
or a forget, starts that rebuild straight away. After a hold from
`POST /facts/quarantine`, it waits until five minutes after the last
hold, so a run of holds costs one rebuild. Before it calls a model, the
rebuild checks `source_fact_ids` against the ledger. It stops there
when none of them is held or gone, and spends nothing. A rebuild that
fails keeps the old profile, which still shows the fact until a later
rebuild succeeds. A restart drops a rebuild that was waiting, so the
service makes the same check when it starts, and rebuilds five minutes
later if it finds one.

Two builds never run at the same time. A second build waits for the
first to save, then chooses its facts again, so it sees every hold the
first one missed. A request for the automatic rebuild that arrives
while one runs doesn't start another. The running one goes round once
more when it's done.

## Maintenance

`POST /consolidate` runs the advisory sweep, async. It covers groups of
exact duplicates and suggestions of facts to pin. It writes nothing at
all. The sweep reads the ledger and returns proposals, including for
exact duplicates, and applying any of them is a separate action a person
takes on the admin page. It needs the owner credential, even on
loopback.

`GET /jobs/{id}` returns this.

```json
{"kind": "distill|summary|consolidate|viz-embeddings",
 "status": "running|ok|failed", "error": null, "result": null}
```

It needs the owner credential, even on loopback, so a caller without one
can start an open async operation and can't read its result. `result` is
the only place an async operation's output lands, and it stays `null`
until `status` is `ok`. Its shape depends on `kind`. A distill returns
mining counts, `{"added", "quarantined"}` always, plus up to four keys
that appear only when they aren't zero.

- `deduped`, when a repeat mine was collapsed.
- `refused_supersede`, when a proposal was refused because the new
  fact's event date is more than a day older than its target's. Old
  claims file as dated history, and never retire newer truth.
- `deferred_supersede`, when a held fact proposed a replacement.
  Quarantine can't change the trusted facts, so the proposal waits for a
  person to review it.
- `unmined`, the messages left unmined because the model refused them,
  or still couldn't finish its reply with more room. Their words stay in
  the chat's history, and the service log names each one.

If another distill of the same conversation was already running, a
distill returns `{"added": 0, "quarantined": 0, "skipped_locked": true}`
and mines nothing. Read the extra keys with a default, and don't index
them, because on the ordinary path none of them is there. A consolidate
returns `{"proposals", "clusters_scanned"}`, and a summary rebuild
returns `{"summary_chars": n}`.

`error` holds the failure message when `status` is `failed`, such as a
missing API key, and a failure is never silent. Jobs live only in the
service's memory, so a restart forgets them, and a later poll is a 404.
Read the result before you restart, or run the operation again.

### Backups

`POST /backup` takes a snapshot now, and `GET /health` reports the
backup state. The service also takes snapshots by itself, at startup
and whenever the newest snapshot is `backup_interval_hours` old by the
clock, as long as the database has changed since. The interval is 6
hours by default, set by `MEMORY_BACKUP_INTERVAL_HOURS`, and `0` turns
the timer off. Time the computer spends asleep counts, and the timer
checks every five minutes, so a snapshot that fell due during sleep is
taken soon after it wakes. `MEMORY_MIRROR_DIR` copies every snapshot to
a second folder.

### Busy probe

`GET /busy` returns `{"busy": false, "reasons": []}`. It says whether a
restart right now would cut off work under way. The fleet's deploy
watcher asks it before every restart, and waits while `busy` is true. It
answers on loopback with no credential, like `/health`.

`reasons` lists fixed labels, sorted, one for each kind of work, and
never an id or content. A running job shows as `distill`, `summary`,
`consolidate` or `viz-embeddings`. `backup` is a snapshot partway
through a copy, whatever started it. `judge` is a judge pass, and
`reembed` is refilling vectors after an embedding model change. The answer comes only from marks kept in the process, so it
never waits on the database. A mark older than an hour stops counting.
The marks live in memory, so only a hung thread can leave one behind,
and the restart this route stops blocking is the cure. The route serves
the watcher and not the chat client, so it sits outside the versioned
contract, and `contract_version` doesn't move for it.

## Admin and visualisation routes, outside contract v1

These serve the owner's admin pages, and may change without a contract
bump. The visualisation routes, `/v1/viz/*`, never return what a fact
says, and a test holds them to it. What they return is geometry: ids,
ages, importance, scores and coordinates. That's what makes the
Mathematics page safe to show.

`GET /v1/viz/recalls` is the one route that returns something readable.
It returns the `query` text of past lookups, which is your own words,
because the live view exists to show what was asked. That includes
history searches, which are gated themselves. No viz route returns what
a fact says, but the recalls feed isn't safe to show until you've
checked what's in it.

That rule covers the viz routes alone. The attachment and summary
version routes in this section do return content, because showing you
your own files and profile text is their job. Who may ask differs. The
attachment routes need the owner credential, and the summary version
routes don't, as [Summary versions](#summary-versions) says.

`GET /` is the admin page, and `GET /math` is the Mathematics page. The
four attachment routes need the owner credential, on loopback too, like
the exact-row fact routes. They return fact content, message bodies and
document text, so a rule for loopback alone isn't enough.

### Attachments

- `GET /v1/attachments` lists every stored file with its conversation.
  `limit` defaults to 200, with a maximum of 1,000.
- `GET /v1/attachments/{id}/file` downloads the original bytes, and
  `?inline=1` shows the file in the browser, for previews.
- `GET /v1/attachments/{id}/preview` shows the file in context. That's
  a text excerpt of up to 4,000 characters, or the kind of image or
  binary file, the message it arrived with, and up to 8 of the newest
  live facts mined from that conversation.
- `DELETE /v1/attachments/{id}` is the attachments twin of the fact
  eraser. A person starts it from the danger zone, and it's the only way
  to delete a file. Bytes stored by content are unlinked only when no
  other row uses them. The file leaves the search index with its row,
  and an image takes its caption with it. It journals to `erasures`, like
  every eraser.

### Messages

`GET /v1/messages/resolve?source_app=&conversation=&message=` turns an
app's reference to a message into the internal row id. The reference is
how Crossband names a message, by its source app, the conversation's
external id and the message's external id. It returns a word for word
preview, and what the erase would touch, which is the live facts that
would move to review and the attached files that would stay. It exists so an app's
erase link can land on the admin page filled in. The admin page reads
`#erase=<source_app>/<conversation>/<message>` and calls it. It needs the
owner credential, even on loopback.

`DELETE /v1/messages/{id}` is the messages twin of the fact eraser. A
voice turn discarded in the app it came from may already be ingested
here, and no automatic path may touch the copy, so this is the person's
hand. It takes one row, has no bulk form, and no app's code ever calls
it. The row leaves the archive and the search index. Live facts bound to
it, because their source turn is this message, are quarantined with a
`source-deleted:` reason and show up in review, where the owner decides
each one. When the live summary was built from one of them, it's
rebuilt straight away. Unbound facts from the same chat aren't touched.
Attachments that came with the message are counted in the response, and
never deleted with it. It journals to `erasures`. It needs the owner
credential, even on loopback.

### Person records

Membro is the fleet's home for who's who. Apps that capture voices
create person records here and upload the clips they've accepted, so a
learnt voice survives an app losing its data folder. Membro never
identifies voices itself, and only records what apps assert. Every route
here needs the owner credential, even on loopback.

`GET /v1/persons?since=<time>` returns the person records changed since
then, with the marks for forgotten people. A syncing app deletes its own
copies of anyone marked forgotten. Each record carries the slug, the
display name and whether the owner set it, the relationship, aliases,
the clip count, `unused_clips`, and timestamps. A name the owner set
survives updates from apps. `unused_clips` came in 1.8, and [Clips an
app has stopped using](#clips-an-app-has-stopped-using) describes it.

`POST /v1/persons` creates or updates a person by slug. Aliases add up.
An alias that already belongs to a different person is refused with a
409, and never moved. A name Membro has seen as a model's speaker label
in the sending app's conversations is refused with a 409. That keeps an
AI in a conversation from ever becoming a person, enforced on the
server too. Facts already tied to `guest:<alias>` link to the person on
upsert, and the response says how many.

#### Clips and corrections

`POST /v1/persons/{slug}/anchors` uploads one clip, in base64. Clips are
stored by content, so the same bytes for the same person do nothing.
Files live under `voice_anchors/`, readable by the owner only. Membro
never prunes clips by itself. A clip goes when the app that uploaded it
deletes it, or when the owner does.

`GET /v1/persons/{slug}/anchors` and `.../{id}/file` list and download
clips, to rebuild an app's lost cache.

`PATCH /v1/persons/{slug}` is the owner's rename, and sets the
relationship. A rename sets the owner flag, so no update from an app
changes the name again. It never creates a person.

`POST /v1/persons/{slug}/anchors/{id}/move`, with `{"to": slug}`, is a
person's correction that this recording belongs to someone else. The
bytes stay, and who they belong to changes. Moving bytes the target
already holds collapses into a delete of the wrongly placed row.
Crossband replays its own moves through this route, so a rebuild can
never bring back a corrected clip.

`DELETE /v1/persons/{slug}/anchors/{id}` deletes one clip. It's
journalled in `erasures`, and the bytes are unlinked when no other row
shares them. Crossband replays its own clip deletes through this route,
and the clips its voice banks drop too. An optional `?reason=`, from 1.8,
says why the app dropped the clip, as `rotation`, `settled` or
`set-aside`. The journal row ends `reason:<value>`, and any other value
is ignored.

`POST /v1/persons/{slug}/merge`, with `{"into": slug}`, folds one person
into another. Aliases, clips and fact links point to the other person.
The losing row stays, marked `merged_into`. It's refused with a 410 when
either side is forgotten.

`POST /v1/persons/{slug}/forget` is the one-press forget. It deletes the
audio from disk, with one `erasures` row that holds no content, and the
person's clip manifests. It marks the person forgotten, and moves their
approved facts back into review as one group of forgotten facts. The
owner decided that nothing is deleted without a word. When the live
summary was built from one of those facts, it's rebuilt straight away.
Afterwards, uploading, listing and downloading clips answer `410 gone`.
Deleting or moving one of their clips answers 404, because none are
left, and moving a clip to a forgotten person is a 410. The record
itself stays listed, so syncing apps learn to delete their copies.

#### Clips an app has stopped using

`PUT /v1/persons/{slug}/manifest`, from 1.8, takes
`{"client": "<app>", "sha256": ["<hex>", ...]}`. That's the clips the
app's voice bank keeps for this person, as sha256 hex digests, at most
1,000. It replaces that app's last manifest, and deletes nothing,
however little it lists. It returns `{"stored": <n>, "unused_clips":
<n>}`. A value that isn't a sha256 is refused with a 422.

A person's `unused_clips` counts the clips that app uploaded, stored
before its manifest arrived, and missing from it. A clip stored after
the manifest never counts. A manifest older than the person's last
change counts nothing until the app sends a fresh one. A change is a
clip moved or deleted, a merge, a rename, or any create or update from
an app, even one that changed nothing. With no manifest the count is 0.

`DELETE /v1/persons/{slug}/unused-clips` and `DELETE /v1/unused-clips`,
from 1.8, are the owner's press on the People page, for one person or for
everyone. The set is worked out again at the press, and never taken from
the caller. Each clip is deleted like the one-clip route, and journals
its own `erasures` row ending `reason:unused`. The answer counts what
went, `{"slug", "deleted", "files_removed"}` for one person, and
`{"deleted", "files_removed", "persons"}` for everyone.

### Summary versions

These three routes aren't gated, unlike the attachment routes, and
answer a caller on loopback with no credential.

- `GET /v1/summary/versions` lists every profile ever built, newest
  first, with metadata only. The history is only added to, so a rebuild
  never destroys a version. Each row carries `word_count`,
  `word_budget`, `model`, `restored_from` and `passes`. `passes` lists
  the rewrite passes that shaped a fresh build, in order, as `expand`,
  `squeeze`, or an empty list. It's `null` on a restore row, and on a
  version stored before the column existed.
- `GET /v1/summary/versions/{id}` returns one version with its full
  text. It's open on loopback, so any program on the computer can read
  any stored profile in full. `GET /summary` is open by the same rule,
  for the current profile, so this widens the reach from "the profile
  now" to "any profile this database has built".
- `POST /v1/summary/versions/{id}/restore` makes that version current
  again by adding a new version row, with `restored_from` set, and
  history is never rewritten. It's open on loopback, so any program on
  the computer can change which profile is live. Nothing is destroyed,
  and you can restore back, but the profile every model reads next round
  can change with no credential.

### Visualisation routes

`GET /v1/viz/decay` returns every fact that isn't superseded, with its
age, importance and score, and the constants of the formula. Held facts
are included, flagged `q: 1`.

`GET /v1/viz/embeddings` returns a cached 3D projection, by principal
components, of up to the newest 2,500 embedded cards. The cap is
`SAMPLE_CAP` in `viz.py`. The projection's Gram matrix costs memory that
grows with the square of the count, so past the cap the newest cards
win. The route returns the cards with their lifecycle timestamps,
superseded ones included, and which of them are in the current summary.
It returns `{"status": "computing"}` while the background projection
job runs. This route and `/v1/viz/landscape` both return `sampled: true`
when the cap was hit, so the page can say "showing the newest 2,500",
and never imply it drew everything.

`GET /v1/viz/landscape` returns the data for "The life of your memory",
one 3D scene that's always current. It needs nothing else first. When
it's cold, it returns `{"status": "computing"}` and starts the one-time
projection build itself, then serves the scene. It returns geometry
only, for the facts that are alive, meaning not superseded and not
quarantined, as they are now.

- `nodes`, each with an id, 3D coordinates `x,y,z`, importance, a
  freshness score and a cluster index.
- `clouds`, the biomes, from k-means on the 3D coordinates, which gives
  the same result every time. There are about the square root of half
  the count, kept between 6 and 14, or one per fact when there are 6 or
  fewer, and empty clusters are dropped. Each cloud carries a 3D centre
  `cx,cy,cz`, the six distinct covariance entries
  `cov=[xx,yy,zz,xy,xz,yz]`, a `spread` radius, mean `freshness`, a
  density `size`, and a palette index. The covariance lets the page draw
  a see-through ellipsoid, by `Σ₂=JΣJᵀ`, and not a flat hull. The
  palette index only tells a biome from its neighbours, and never maps a
  topic to a colour.
- `edges`, facts recalled together in the access log, weighted.
- `sediment`, for each current fact, the ids, dates and importance of
  the facts it superseded.
- `summary_ids`, the current summary's members, for the ring of dots.
- `sampled`, true when the 2,500 card cap was hit, as on
  `/v1/viz/embeddings`.
- `notes`, naming any layer the current ledger can't fill yet.

The admin page draws "The life of your memory" from this route alone.
`/v1/viz/embeddings` still answers, and the page doesn't use it.

### Recall trace and the access log

`POST /v1/viz/recall_trace` takes `{"query", "limit"}` and returns the
recall pipeline, with each step shown. Every card gets its score parts
and its fate, which is kept, dup, collapsed, over or dim, plus lifecycle
and duplicate edges, so the page can replay the answer as of any past
time. A test keeps its scoring in step with `/v1/recall`. It ranks every
valid fact, though, including facts bound to one conversation. `limit`
defaults to 20, and any value up to 500 is cut to 20, because the route
exists to draw a diagram, and not to export data. A value outside 1 to
500 is a 422.

`GET /v1/viz/recalls?after=<ts>` returns lookup events from the access
log, which lasts across restarts. That's recalls, history searches and
the summary fetched each round, and they feed the live view. It isn't
gated. `query` is the caller's own question word for word, its first
200 characters. That makes it the one `/viz/*` route that hands readable
text to a caller on loopback with no credential.

Each event is `{ts, kind, origin, query}`. `kind` is `recall`, `search`
or `summary`. `origin` is `http`, `auto` for a recall or history search
a client fired on the user's behalf, or `mcp:<client-name>`. `POST
/recall` and `POST /search` both take an optional `origin`. The MCP
adapter's processes write to the same log under `mcp:<client-name>`, so
lookups from outside tools appear too. The `access_log` table is only
ever added to, like the ledger. Each row records when, what was asked,
and which facts came back, as ids and scores, and the service never
updates or deletes a row.

## MCP adapter, the tools for models

The tools are `recall_memory`, `save_memory`, `search_history` and
`memory_summary`. They're library calls inside the same process, against
the same SQLite file, and never HTTP. `memory_service/mcp_server.py`
imports `recall`, `ledger`, `episodic` and `summary` directly, and opens
`data/memory.db` itself. The tools follow `/recall`, `POST /facts`,
`/search` and `/summary`, and no request is ever made to the service.

- `recall_memory` asks for a fixed 20 facts, global ones only, and never
  superseded ones.
- `save_memory` drops a line about the memory system's workings, or
  about building software, which `POST /facts` never does. An
  `event_date` it can't read is ignored where `POST /facts` would refuse
  it. One it can read is kept as a calendar day, the same as
  `POST /facts`. It has no web, guest or conversation stamps.
- The adapter needs to read and write `data/`, and `MEMORY_DATA_DIR`
  must point at the same folder the running service uses. Point it
  elsewhere, and saves land in a different ledger that never shows up on
  the admin page.
- It keeps working while the HTTP service is stopped. The database must
  already exist, because the adapter connects, and doesn't build or
  migrate the ledger's schema, which is the service's job at startup. The
  one exception is the `access_log` table. If it's missing, the adapter
  creates it on its first write, so lookups against a database that
  hasn't been migrated are still recorded, and never lost.
- Its calls never pass through the API's loopback and token checks, and
  file permissions on `data/` govern access. The one exception is
  `search_history`, which checks `MEMORY_AUTH_TOKEN` itself, to match
  the owner gate on `POST /search`. It compares its own
  `MEMORY_AUTH_TOKEN` with the token in the service's `config.json`,
  `config.local.json` or `.env`. Register the server with
  `-e MEMORY_AUTH_TOKEN=<the service's token>` for search, and the other
  three tools need no token. With no token in those files, the check is
  skipped, and `search_history` answers anyone who can run it.

Every save carries `origin_agent = "mcp:<client-name>"`, so it's
quarantined automatically. The client name comes from
`MEMORY_MCP_CLIENT`, and it's `claude-code` by default. Missing the HTTP
hop doesn't weaken the write gate, because the gate lives in
`ledger.add_fact`, not in the API layer. The admin operations, approve,
dismiss, delete, ingest and consolidate, aren't offered over MCP. An
outside tool can propose facts, and can never approve, delete or
otherwise change the trusted facts.

`memory_summary` puts a freshness header before the profile's prose.
That's a `generated_at` stamp in UTC, and a one-line reminder to check
anything time-sensitive, or the status of an active thread, with
`recall_memory`. It's added text, and no new wire field. A stale summary
reads as if it were current, so the model reading it must be able to
see its age without a second call.

## Admin MCP adapter, read-only and opt-in

`memory_service/mcp_admin_server.py` is a second MCP server, separate
from the four tools, and not registered by default. It exists for a
session cleaning up the ledger, like one confirming a fact it suspects
was mined wrongly, which needs exact rows and not recall by meaning. It
has two tools, `search_facts(query, status)` and `review_queue(query)`,
which are thin wrappers that only read, over `GET /v1/facts` and
`GET /v1/review`. Both routes are gated by the token on the server, as
[Always gated](#always-gated-even-on-loopback) lists, so the wrapper's
need for a token is enforced by the API. It's never only a habit of the
client. It differs from the four model tools in these ways.

- It needs `Authorization: Bearer <MEMORY_AUTH_TOKEN>` matching the
  running service's own admin token, even on loopback. The base API
  trusts loopback for its open routes, but these two routes return exact
  ids and review state, which reveal more than recall's paraphrased
  output.
- It has no save tool, and no write tool of any kind. Approve, dismiss,
  edit and delete stay with a person, on the admin page or the HTTP API.
- It talks HTTP to the running service at `MEMORY_API_URL`, as its own
  process and connection. It never opens the SQLite file, so it works
  the same whether the calling session shares a filesystem with the
  service or is fully sandboxed from it.

## Invariants

The tests hold the contract to these.

1. Facts are only added to. No automatic path deletes a fact, which can
   only be superseded, quarantined or dismissed.
2. The episodic record is the ground truth, and no maintenance pass ever
   changes it.
3. Quarantined facts never appear in `/recall`, and never feed a
   summary built after they were held. A summary built from a fact
   that's since been held or erased is rebuilt without it, as
   [Summary](#summary) describes. Until that rebuild saves, the live
   summary still shows the fact.
4. A write from an untrusted origin is always quarantined when it's
   created.
5. Every fact carries `event_date`, which is never null, and
   `origin_agent`.
6. A `/summary` claim traces to `source_fact_ids`, and each of those
   facts' raw `origin_agent` and `source` is exposed through
   `provenance`. The summary's prose never names a speaker for a fact
   whose origin doesn't mechanically support one.

## Development

`GET /v1/disposable-identity` supports throwaway benchmark stores. It
reports `disposable: false` on a real store, and returns no token. The
harness that uses it is [bench_memory](../bench_memory/README.md).
