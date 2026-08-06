# Changelog

House convention: user-visible change, one line each, newest first.

## Unreleased

### Initial public release

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
