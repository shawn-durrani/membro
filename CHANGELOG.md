# Changelog

House convention: one entry per user-visible change, newest first. Keep an
entry to a short paragraph; the issue holds the detail.

## Unreleased

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
