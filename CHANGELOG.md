# Changelog

House convention: user-visible change, one line each, newest first.

## Unreleased

- Miner src= retry (#35): in a session with guest or unrecognised
  speakers, a fact the miner failed to bind to its source turn now gets
  one batched corrective re-ask before the fail-safe hold, the same
  posture the importance score already had. Facts from your own turns
  land as canon again instead of flooding the review queue under one
  generic reason; facts a retry binds to a guest's turn still hold with
  the guest named. Nothing about the walls or what quarantines changed.
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
  unchanged. Passkeys are per web address — enrol `http://localhost:8901`
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
