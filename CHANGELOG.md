# Changelog

House convention: one entry per user-visible change, newest first. Keep an
entry to a short paragraph; the issue holds the detail.

## Unreleased

## v0.2.0 (2026-10-02)

- A history search that matches words in a photo's caption now shows the
  caption around the match (#160). The search found the right photo, but
  its hit came back with a blank excerpt, so an AI saw only the file name
  and not what the photo showed. The excerpt is the same 64 words, marked
  the same way as a message's, and the hit's speaker reads
  `file: <name> (image caption)` so the AI knows what it's reading.

- Mining no longer loses facts on a model that thinks before it answers
  (#167). Claude Sonnet 5.5 and the other newer models think unless a
  request turns it off, and the thinking shares the answer's room, so
  the miner's reply could run out partway and the rest of the chat went
  unmined. The miner still thinks on a model that does by default, as it
  was benchmarked, and now gets 4,000 tokens for it on top of the room
  for its facts. Its retries, captions, the sweep and the judge's
  witness check turn thinking off where the model allows it, and get
  more room where it can't be turned off. Every model call checks how
  its reply ended. A miner reply cut short or refused is mined again in
  smaller pieces, and a retry cut short holds its fact for review. The
  judge's roleplay check asks each model for the thinking it takes, so
  `judge_model` can name any model. Claude Haiku 4.5 gets the same
  requests as before.

- A history search can now say it was run by the chat app on your
  behalf, the way an automatic recall already does. `POST /search`
  takes an optional `origin` field, and `auto` marks such a search in
  the access log. The live view on the Mathematics page shows it as the
  app preparing context, so it no longer reads as a model choosing to
  dig into your past chats.

- A profile the model didn't finish is no longer saved (#163). The
  profile writer thinks before it writes, and on 2 of 20 benchmark builds
  its thinking used up the 8,000-token cap, so the profile stopped
  mid-sentence and lost its newest sections. Each build call now gets
  16,000 tokens, or four per word of the budget if that's more, and the
  writer checks how every reply ended. A draft cut short keeps the
  profile you had, and a cut-short expand or squeeze pass keeps the
  draft it started from. The service log says why, with no profile text
  in it, and a rebuild started from the admin page reports the reason.

- A one-off repair gives mined facts back the message they came from
  (#146). From 13 August the miner stored most facts without it, because
  it couldn't read the model's bracketed tags (#145). Erasing that message
  then left the fact in recall instead of sending it back to review, and
  any date the model gave it was lost. `scripts/relink_unbound_facts.py`
  binds such a fact to one of your own turns. It does that only in a chat
  with no guests, and only when that turn clearly shares the most
  distinctive words with the fact. It moves the date only when that turn
  writes the fact's calendar date. It's a dry run unless you pass
  `--apply`, which refuses while membro is busy, keeps a copy of the
  database first, and notes each change without any text in
  `data/repairs.jsonl`.

- The miner now keeps what you mention in passing about what you own and
  use, what you've done, your home and your family, even in a chat about
  something else (#136). It used to drop an aside like the software you
  edit with or how many sisters you have as part of a one-off task. It
  still leaves out one-off numbers, like what one purchase cost. Its
  template now writes values without angle brackets, so the model has
  none to copy. On the benchmark's 20 preference and multi-session
  questions, three more came right from facts it now keeps, and each
  history's ledger nearly doubled, from 38 facts to 74.

- You can now erase a fact the automatic judge has looked at (#158). The
  judge keeps a record of each look that points at the fact, and the
  database refused to delete a fact while that record stood, so the
  eraser failed with a server error. The eraser now removes that record,
  which holds only an id and a time, along with the fact.

- When search can't use its index, each hit now shows the words around
  the match instead of the message's first 200 characters (#156). That
  fallback serves a query the index can't parse, and every search while
  the index is out of step before a restart repairs it. Its excerpts are
  now the same 64 words, centred on the match and marked the same way.

- Erasing a photo now takes its caption's words out of the search index
  too (#154). Membro indexes a photo's auto-generated caption so you can
  search for what it showed, but the file eraser only removed the photo's
  empty text, so the caption's words stayed in the index and in later
  backups. Search never showed the erased photo, and restoring a snapshot
  had the same gap. Nothing on this install needs repair: no file had been
  erased yet.

- History search now matches word endings (#152). A search for "sister"
  finds "sisters", and "packed" finds "packing" and "packs". Both search
  indexes, for messages and for file text, are rebuilt with the new rule
  the first time the service starts. On a store the size of a busy one
  (about 43,000 messages) that takes about a second. Image captions stay
  searchable through the rebuild, which a plain index repair used to lose.

- A history search now shows up to 64 words of each matching message, up
  from 24 (#150). The shorter excerpt often stopped just before the detail
  the search was for, like a number or a price, so an AI found the right
  message and still couldn't answer. A hit averages about 330 characters
  now, up from about 140.

- The roleplay check now reads only what people said in a chat (#148). It
  used to read the AI's replies too, so ordinary advice like "rehearse your
  set" held every true fact the person had shared in that chat, as if it
  were roleplay. Framing from the person, a guest or any speaker membro
  can't place still holds facts, as before.

- The miner now reads its tags when the model copies the template's angle
  brackets, like `src=<3> importance=<7>` (#145). It couldn't read that
  form, so it asked the model again for every such fact's importance, and
  it stored the fact without the message it came from, which also dropped
  any date the model gave. In one benchmark run that was 459 of 770 fact
  lines. A placeholder copied whole, like `event=<YYYY-MM-DD>`, still reads
  as no date.

- The People page is easier to use on a phone. Each person's details use
  the full width of the card, a line divides one person from the next, and
  the rename box and merge picker each share a line with their button. A
  voice clip's player fits beside its delete button instead of running off
  the card. The desktop layout is unchanged.

- The miner no longer sees other chats' guest facts (#139). When it mines
  a chat, it's shown your existing facts so it doesn't save one twice, and
  that list held every chat's facts, guests' facts included. It now holds
  what a recall from that chat would find: the facts every chat can see,
  and the ones bound to this chat. So a guest's fact from one chat can't
  pass for a repeat, or be marked replaced, by something said in another.
  The list keeps its cache-friendly layout, so a burst of mining calls
  still shares the prompt cache.

- Tapping a text field on a phone no longer leaves the admin page zoomed
  in. iOS Safari zooms in on any field under 16px and stays there, so every
  field you type into is now 16px on a touch screen, including the
  password box on the locked page and in landscape. A long word in a held
  fact now wraps instead of being cut off, and the People page's "merge
  into" picker fits its row. The desktop look is unchanged.

- Mining and the profile now reuse Anthropic's prompt cache when their
  calls come close together (#137). The miner's instructions and its list
  of your existing facts are laid out to stay the same from one call to
  the next, so in a burst (a backlog, an import) every call after the
  first reads them at a tenth of the price. The same facts, rules and
  answer format reach the model. A profile build's expansion pass reads
  the entries its draft just sent. Each model call also writes one line
  to `data/service.log` with its call site and token counts, never text,
  so you can see how much of the input came from the cache.

- The LongMemEval benchmark harness is in the repo, under `bench_memory/`.
  It gives each question a fresh copy of membro and crossband, started
  from git worktrees with their own data folders and ports. It loads that
  question's chat history into membro, asks crossband in a new chat, and
  grades the answer with LongMemEval's own prompts. Your real apps are
  never touched. A mock run is free and sends nothing off your computer.
  A paid run meters every model call and stops before the budget you give
  it. At list prices a question costs about 35 US cents, most of it membro
  mining the history. A 60-question sample, ten of each type, got 33
  right, and its table is in `bench_memory/METHODOLOGY.md` (#9).

- Backups are private to your own account on the Mac. The database was
  already, but each backup, the backups folder and the service log could
  be read by any other account once it got past the data folder, and a
  backup copied elsewhere kept that open mode. At startup the service now
  takes that access away from everything in the data folder, and every
  file it makes after that is private from the start. The restore and
  the maintenance scripts do the same (#133).

- A restart or a deploy no longer signs you out of the admin page
  (#131). Sessions used to live only in the service's memory. They're
  now kept in the database as a SHA-256 hash with their 24 hour expiry,
  never the cookie itself, so a copy of the database can't sign anyone
  in. "Log out" still ends that session. A password reset now ends
  every other session too, where before only a restart did, and so
  does removing a passkey, so a lost phone's session goes with its
  passkey. `scripts/restore_snapshot.py` ends every session as well.
  The first start on this version signs everyone out once.

- Restoring a snapshot no longer brings back voice clips you deleted or
  people you forgot (#129). The restore replayed the erasures made since
  the snapshot but skipped the voice ones, so a deleted clip came back as
  a row with no audio behind it, and a forgotten person came back
  unforgotten. It now replays those too: every clip delete, including
  each one from the "clips crossband no longer uses" button, and every
  forget, with the person's clips, kept-clip lists and approved facts
  handled as the forget handled them. It then lists any clip whose audio
  file is missing, by person and clip id, and keeps it for you to judge.
  The dry run now counts the erasures it would replay by kind.

- Memory contract 1.8 (#127): the People page shows, for each person, how
  many stored voice clips crossband no longer uses, with a button to
  delete them, and a total with one button for everyone. Membro kept every
  clip crossband ever sent, so one person's voice could fill hundreds of
  clips where crossband uses 15. Crossband now sends the list of clips it
  keeps each time it syncs, and the count is the stored clips missing from
  it. The list alone deletes nothing: a clip goes only when you press, and
  each one leaves a content-free line in the erasure journal. A clip
  crossband drops is deleted as an ordinary clip delete, which now says
  why it went (rotation, settled or set aside) in the journal. A 1.7
  client keeps working, and without a list nothing is counted.

- Your other apps are one tap away (workbench#100). A row at the top of
  the admin page links Crossband, Spendglass and Threadfold. Membro asks
  each one on this machine where a browser can open it, keeps the
  answers for a minute, and leaves out any app that doesn't answer. On
  the Mac every running app shows. From your phone only the apps served
  on your tailnet show, so the row never offers a link that won't open.
  The owner-only `GET /app-links` builds the row, and a new
  `sibling_apps` setting names the apps. The wire contract is unchanged,
  so `contract_version` stays where it is.

- Backups no longer stall while the Mac sleeps. The six hours between
  automatic snapshots used to count only time the Mac was awake, so a
  laptop that slept most of the day could go days without a new restore
  point. The timer now checks every five minutes and goes by the clock,
  so a snapshot that fell due during sleep is taken within five minutes
  of the Mac waking. An unchanged database still gets no new copy (#123).

- Memory contract 1.7 (#115): a fact a model saves while a guest is in
  the room now stays in that chat once you approve it. It was held for
  review, but after approval it was recalled in every chat, because the
  save never said which chat it came from. `POST /facts` now takes the
  same `source_app` and `conversation_id` pair as `/ingest` and
  `/recall`, and a save carrying `guest_speakers` binds to that chat,
  like a fact mined from a guest's own words. A save made before the
  chat's first handoff creates the chat's record, so it binds from the
  start. Your own saves stay global. A 1.6 client keeps working
  unchanged, and its guest-present saves stay global as before.

- Membro's two MCP servers now run on version 2 of the MCP SDK, which
  renamed the server class they use from FastMCP to MCPServer. Their
  tools, names and answers are unchanged. `start.sh` installs the new
  SDK the next time the service starts. Until then, an MCP server
  launched from the old virtualenv fails to import, so restart the
  service before you open a new session (#109).

- The MCP server's word-for-word chat search now refuses a session that
  wasn't given the owner's token, even when that token lives only in
  `.env`. It used to check against its own empty copy and let everyone
  through, and a token passed at registration was checked against itself.
  Register the sessions that should search with `-e MEMORY_AUTH_TOKEN`,
  as the docs say (#117).

- Membro no longer writes your recovery secret into its log. It used to
  print the secret on every start, and under launchd that output goes to
  `data/service.log`, so each restart added a plain-text copy. Now a
  configured `MEMORY_AUTH_TOKEN` is never printed, and a token minted for
  one start is printed only on a first run, before you've set a password.
  Copies already in an old log stay there until you clear the log (#113).

- The judge pass no longer locks other writes out while it waits on the
  model. It used to hold the database's write lock for the whole pass,
  so a slow network, or the computer sleeping mid-pass, made incoming
  chats fail with "database is locked" and history searches hang. Each
  fact's verdict now saves straight after its own model call. A search
  whose access-log write meets a busy lock also stops waiting twice
  before it answers (#111).

- Every fact now carries a scope, and a guest's fact stays in its own
  conversation (#72, contract 1.6). A fact drawn from a guest's turn, or
  saved while guests were in the room, is recalled only from the
  conversation it came from and never joins the profile. Your own facts,
  and everything stored before this, are global, so recall behaves as it
  did. A client names its conversation on `/recall` to get the facts
  bound to it. The review queue says which chat a bound fact belongs to,
  approving keeps the binding, and "Recall everywhere" is the one way to
  widen it.

- Restoring a snapshot no longer brings erased things back (#101). A
  snapshot is a point in time, and the erasure journal lives inside the
  database, so copying an older snapshot into place used to resurrect
  every fact, message and attachment erased since, tombstones and all.
  The new `scripts/restore_snapshot.py` keeps a copy of the live database
  first, copies the chosen snapshot in, then replays every journalled
  erasure the older copy lacks through the same erasers the danger zone
  uses, and appends those tombstones under their original times. It
  refuses while the service is running and prints counts, never content.

- The leak scanner is now a byte-for-byte copy of crossband's canonical
  (#99), and the suite fails when the copy drifts from it. The copy
  brings crossband's fixes: placeholder allowlists anchored to the whole
  token, so a masked tailnet no longer excuses a machine name in front
  of it; an inline `secret-scan: allow` marker for a deliberate keep; and
  tree hits that name the file they came from.

- The profile now aims for most of its word budget (#96). The prompt
  used to give the model a ceiling and a warning against padding, and
  the profile settled at about half of its 2,000 words while the ledger
  held far more than that. The prompt now asks for a range, 80% of the
  budget up to the budget (`memory_summary_fill`; `0` turns the floor
  off), spent on the specifics the entries carry: dates, names of
  projects and places, numbers, the current state of each thread. A
  draft under the floor gets one expansion pass from the same entries,
  only when they hold at least twice the floor in words; a draft still
  short after that is kept, and nothing is invented. Each stored version
  now records which passes shaped it, and the admin page shows the fill
  as a percentage.

- `GET /v1/busy` (#95): whether a restart right now would interrupt work
  in flight, for the fleet's deploy watcher, which asks before a restart
  and waits while the answer is true. Open on loopback like `/v1/health`.
  `reasons` carries fixed labels only: a running distill, summary,
  consolidate or embeddings-projection job, a backup mid-copy, a judge
  pass, or the re-embed refill after an embedding model change. A mark
  older than an hour stops counting, so a hung thread cannot hold every
  deploy. The route sits outside the versioned client contract, so
  `contract_version` stays at 1.5.

- Memory contract 1.5 (#93): a model's direct save now says who was in
  the room. `POST /facts` may carry `guest_speakers`, the same
  `guest:<name>` and `guest:unknown` values `/ingest` uses, and a stamped
  save is held for review under a new group, "Saved while a guest was
  in the room". The mined path already held a guest's words because
  each message names its speaker; the save tool named nobody, so a
  guest's claim relayed by a model reached canon unheld. A save that
  also read the web keeps its web-derived group with the guest clause
  appended. A 1.4 client keeps working unchanged.

- Memory contract 1.4 (#84): four additive changes so a client app can
  close seams the 2026-08-28 fleet audit found. `/search` hits carry
  `web_sources`, the domains the authoring turn read from, so archived
  web text can be marked untrusted when read back. `/health` reports
  `browser_origin`, the address a phone can open this service at, so a
  client links the eraser somewhere that works. A new
  `/conversations/{app}/{id}/watermark` route reports the highest
  message id held (erased ones included), so a client can wind its
  ingest watermark back after a restore instead of leaving a silent
  hole. `event_date` is now a calendar day at the owner's local
  midnight for every writer, with same-day recall ties broken on the
  save time. A 1.3 client keeps working unchanged.

- Verbatim transcript search now needs the owner token on every path
  (#81). The MCP server's search_history refuses when the server was
  registered without MEMORY_AUTH_TOKEN, matching the HTTP gate; recall,
  summary and save are unchanged. Re-register the server with the token
  to keep search available to your own tools.

- Two open PRs no longer conflict on the changelog (#79). Each change
  now ships its entry as one file under `changelog.d/`, and a release
  folds them into `CHANGELOG.md` newest first, above the entries already
  sitting under Unreleased. A test fails any PR that edits Unreleased
  directly and names the new home.

- Person records now report when they last changed (#73). The sync
  endpoint filters on that stamp, but the records themselves never
  carried it, so a syncing app could not learn the newest stamp it had
  seen and re-read every person on every pass. The field the API doc
  already promised is now in the record.

- A crash loop can no longer destroy your restore points (#75). Every
  startup snapshots the database before migrations, retention keeps the
  newest 14 copies, and the service manager restarts a crashing app
  every few seconds - so about two minutes of crash loop used to evict
  every pre-crash snapshot at exactly the moment one was needed. A
  snapshot byte-identical to the newest one is now discarded, so
  restarts that change nothing keep the history intact.

- Other apps' passkeys no longer crowd membro's unlock sheet (#70).
  The fleet's apps share the browser's localhost passkey scope, so the
  sheet used to offer every app's key here. The gate now tells the
  browser exactly which keys are membro's own. The trade, made
  deliberately: the lock screen now names those key ids to anyone who
  can reach it - an id is a serial number for the key, not a secret,
  and it cannot unlock anything.

- Your passkey now says whose it is (#68). The system account
  picker lists every localhost app's passkey in one sheet, and
  membro's row used to read as a bare "owner". New enrolments
  register as "membro owner". An existing passkey keeps its old
  label until re-enrolled: remove it in the admin Passkeys panel,
  then enrol again.

- Transcript search survives an emptied search index (#38). An
  empty-but-valid FTS index answers every query with zero rows without
  raising, so drift read as a genuine no-match and the old fallback (it
  only fired on exceptions) never ran. When a query comes back empty,
  the query path now checks the index's shadow table: an empty index
  over a non-empty record serves the bounded substring fallback and
  logs the drift. Healthy no-match searches pay one O(1) probe and
  return an honest empty result; the startup repair still rebuilds the
  index itself.

- Embeddings can run locally too, and model changes are space-safe
  (#60). `embedding_base_url` points memory search at any
  OpenAI-compatible server, no key required. The active embedding model
  is stamped beside the vectors; changing it drops every stored vector
  atomically, re-embeds in the background, and serves keyword-only
  search until the rebuild completes. Vectors from different models are
  never compared, and a stale-dimension vector is ignored, not scored.

- A judge can now prove a held fact innocent (#58, off by default).
  With `judge_pass` on, a background sweep re-reads grounding holds and
  clears one only when the model quotes a verbatim excerpt of the chat
  that supports each flagged term, verified by the wall's own matching
  rules. Persona-flagged chats judged "technical discussion" are only
  relabelled into their own review group for one-click bulk clearing,
  never approved. Any judge failure leaves a row exactly as it was.

- Local models are a first-class choice for the utility layer (#59).
  A new `llm_base_url` setting points every non-claude model name at an
  OpenAI-compatible server (Ollama, MLX, LM Studio); with it set, no API
  key is required for that branch. Defaults are unchanged, cloud key
  checks still fail loudly, and a dead local endpoint fails loudly too.

- The grounding wall stops holding facts for three mechanical reasons
  (#57). A word capitalised only because it starts a sentence is no
  longer read as a name. "Wi-Fi" now grounds off "wifi" and short
  hardware tokens ("M2", "GB", "ARM") ground on word boundaries. The
  allowlist now includes every person name and alias membro already
  holds, so a nickname in chat grounds the full name in a fact. An
  entity genuinely absent from the source chat still holds.

- A fact learnt from a web page is held for review (#55, contract 1.3).
  Messages on ingest and explicit saves may carry `web_sources`, the
  domains a web tool read in the round that produced them. A fact born
  from a stamped turn, or saved with a stamp, quarantines with a
  `web-derived:` reason naming the domains, and the review queue groups
  these holds under "Learnt from a web page" with the usual per-group
  bulk actions. Older clients that never send the field keep exactly
  the 1.2 behaviour.

- The wire knows who spoke (#33, final slice; contract 1.2). A message
  arriving on ingest may carry the sending app's structured belief -
  which person record, how confident, and how it knows. Facts mined
  from such a message link to the person automatically when the
  identity is human-confirmed (introduced, owner-corrected) or a
  strong voice match; weaker guesses never auto-link. Old clients are
  untouched: without the field everything behaves exactly as 1.1.

- People are manageable from the admin page (#33, slice 3): rename
  (apps can't change it back), merge duplicates (spellings, clips and
  linked facts move across), listen to any stored clip, move or delete
  a single recording, and forget - each action saying plainly what it
  does before it does it. Clip moves and deletes are the same
  corrections crossband replays here, so the durable record and a
  future rebuild always reflect your judgement.

- Person records land (#33, first slice): membro is now the fleet's
  durable home for who-is-who. Capture apps create people and upload
  their voice clips (content-addressed, owner-only, kept in full);
  guest facts link to the person on sight of a matching alias; a
  one-press forget deletes the audio, marks the person forgotten for
  every syncing app, and moves their approved facts back into review
  as one group - nothing silently deleted. A model's name can never
  become a person, enforced server-side too.

- The owner can erase one archived message (#45), finishing what
  crossband's discard starts: a voice turn deleted at the source may
  already have a copy here, and nothing automated may touch it. The
  danger zone gains a third eraser (and producers can deep-link it
  prefilled, with a preview and the blast radius shown before you type
  DELETE). Facts mined from the erased message move to review instead
  of vanishing; attached files stay for their own eraser. Every human
  erasure now leaves a content-free journal row: what was erased is
  gone, that it was erased is not.

- The review queue groups by hold reason, and one decision clears a
  cause (#34). Every held fact carries a stable reason class (said by a
  guest, couldn't prove who said it, external write, and so on); the
  admin page shows the queue grouped by it, largest cause first, with
  approve-all and dismiss-all per group acting on exactly the rows on
  screen. Owner-only, like every review action - nothing automated can
  approve anything.

- Review evidence, and provenance only where it is real: a held fact now
  shows the turn it came from (who said it and what they said) in the
  review queue, on the admin page and in `GET /review`. A fact the miner
  could not tie to one turn is stored unbound and says so, instead of
  being pinned to whichever message happened to end the mining window.
  And a src= binding the miner supplies on the corrective retry is now
  checked against the turn it names, so a guest's sentence can no longer
  be quietly approved as if you had said it. Nothing about what
  quarantines changed.
- Miner src= retry (#35): in a session with guest or unrecognised
  speakers, a fact the miner failed to bind to its source turn now gets
  one batched corrective re-ask before the fail-safe hold, the same
  posture the importance score already had. Facts from your own turns
  land as canon again instead of flooding the review queue under one
  generic reason; facts a retry binds to a guest's turn still hold with
  the guest named. Nothing about the walls or what quarantines changed.
- FTS desync self-repair (#37): a search index that has fallen out of
  step with the stored messages (dropped or recreated, so every search
  returned zero rows without erroring) is now detected and rebuilt
  automatically at startup. `/health` reports `fts_in_sync` inside the
  contractual `db` block and goes `degraded` while it is false, and
  `scripts/rebuild_fts.py` repairs a live instance without a restart.
  Messages, attachments and the fact ledger are untouched - the repair
  only rebuilds the derived index.
- Guest speakers (#31): ingest accepts `guest:<name>` and `guest:unknown`
  speaker values beside `user` and the model slugs (additive, contract
  version unchanged). Facts mined from a guest's speech are held for
  review with the guest's name in the stated reason, phrased in third
  person as facts about the guest, never absorbed into the owner's
  first-person profile. Speech from an unidentified or unrecognised
  speaker is treated as untrusted the same way, so nothing new gains
  trust by default; the owner's own turns are unchanged.
- Mobile view (#29): the admin page, both lock screen layouts, and the
  Mathematics page are now usable at phone width. The lock screen tells
  phones its true size (viewport tag), controls stack and grow to touch
  size, form text is large enough that phones no longer zoom in on tap,
  and tables scroll sideways inside their own frame instead of
  stretching the page.
- Passkey login (#27): enrol a Touch ID / iCloud Keychain passkey from the
  unlocked admin page (Admin → Passkeys) and the lock screen offers it first,
  password one click behind it. The password and recovery secret are
  unchanged. Passkeys are per web address: enrol `http://localhost:8901`
  and each trusted host separately; `127.0.0.1` cannot hold one (a browser
  rule: an IP address is not a valid WebAuthn relying party).

## v0.1.1 (2026-08-07)

- SECURITY.md now names the complete set of routes open on loopback:
  the profile version list, the disposable-identity probe, and profile
  regeneration were missing from the stated lists.
- TUNING.md documents that trusted hosts can be set by config key as
  well as environment variable, and their different shapes (JSON list
  vs comma-separated).
- Docs state the credential gate once, in one place, and the import
  command in the README is the one that works.

## v0.1.0 (2026-08-06)

First public release.

- Local-first memory service: immutable episodic record with FTS,
  append-only fact ledger with temporal validity, and a continuously
  rebuilt profile summary with per-fact provenance and version history.
- Deterministic extraction walls (grounding, temporal grounding, source
  trust) with a reviewable quarantine queue; system and builder-process
  chatter dropped rather than queued.
- Hybrid recall (semantic + keyword + bounded recency) with paraphrase
  collapse; verbatim history search; importance-weighted selection with
  an owner-only permanence tier.
- claude.ai export importer (idempotent; multiple accounts import
  cleanly side by side).
- MCP server with four model-facing tools; every MCP save is quarantined
  by origin. Separate opt-in read-only admin MCP pair.
- Owner auth: durable password login with opaque revocable sessions;
  out-of-band recovery secret; exact-row endpoints gated even on
  loopback.
- Attachments with content-addressed storage, text extraction, optional
  vision captions under strict grounding rules.
- Self-scheduled snapshots with rotation and an optional mirror folder;
  launchd supervisor install script; loopback-only by default with
  deliberate tailnet-only widening.
- Keyless degradation throughout: no API keys, full test suite passes.
