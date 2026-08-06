# Architecture

Membro ingests conversations into an immutable episodic record, mines
them asynchronously for facts that pass deterministic extraction walls,
and rebuilds a profile summary from the current ledger. A loopback HTTP
API and an MCP server sit on top; one SQLite file under `data/` holds
everything. The design goal: you can always audit what the system
believes and where each belief came from. The decisions below are
settled.

## The shape

```
api.py        - the HTTP contract (docs/API.md); loopback by default
mcp_server.py - four model-facing tools; saves quarantined by origin
episodic.py   - immutable ingested messages + FTS; ground truth
mining.py     - the reflection pass: transcript to candidate facts
walls.py      - deterministic extraction walls
ledger.py     - append-only facts with temporal validity
weighting.py / summary.py - selection and the rebuilt profile cache
db.py         - SQLite (WAL), snapshots, integrity checks
```

## The ledger is append-only

No automated path hard-deletes a fact. Supersession, quarantine, and
dismissal are reversible state changes. The single hard delete is one
API handler behind the owner credential. Ingested messages and the
access log follow the same rule.

## The transcript is ground truth; the summary is a cache

Ingested messages are never modified. The summary is rebuilt from valid
facts, records the fact ids that produced it, and appends each
regeneration to a restorable version history.

## Writes are gated by shape, not claimed identity

Only the literal `user` sentinel and registered `source_app`s write into
canon. An `mcp:*` origin is never trusted, even when it claims a
registered app, because identity over local stdio is self-asserted.
Untrusted writes are stored and quarantined, not rejected.

## Every mined fact passes deterministic walls

Grounding, temporal grounding, and source trust quarantine a doubtful
fact for review. System-meta and builder-process chatter is dropped
instead, since it is not biography and would flood the queue.

## Dates are never invented

`event_date` is honoured only when the extractor names the single
source message and that message contains the calendar date. A missing
year comes from the conversation, not the model. Ungrounded dates fall
back to conversation time.

## Selection is recency times importance; permanence is a tier

Facts decay on their event date with a half-life stretched by
importance (roughly four times longer at 9 than at 1). Durable and
active pools are selected separately; near-duplicates collapse before
selection. Importance 10 never decays, and only the owner can assign
it; the miner is capped at 9.

## Open endpoints are defined by their projection

`/v1/recall` answers unauthenticated loopback callers because every
chat round calls it, so it returns exactly six documented fields and at
most 50 rows. Everything reading or writing exact rows (facts, review,
search, attachments, jobs) requires the owner credential even on
loopback. Sessions are opaque server-side ids in httpOnly cookies; the
bearer token never rides in a cookie.

## The word budget is enforced by rewriting

An overlong summary draft gets one compress pass; every failure mode
keeps the complete draft. Truncation would cut the newest sections
first, so it is never used.
