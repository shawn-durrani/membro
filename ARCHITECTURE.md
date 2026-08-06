# Architecture

Membro ingests conversations into an immutable episodic record, mines
them asynchronously for durable facts that pass deterministic extraction
walls, and rebuilds a profile summary from whatever the ledger currently
holds. A loopback HTTP API and an MCP server sit on top; one SQLite file
under `data/` holds everything. The design optimises for one property:
you can always audit what the system believes and where each belief came
from. Below are the decisions that are settled and unlikely to change.

## The shape

```
api.py        - the HTTP contract (docs/API.md); loopback by default
mcp_server.py - four model-facing tools; saves are quarantined by origin
episodic.py   - immutable ingested messages + FTS; ground truth
mining.py     - the reflection pass: transcript -> candidate facts
walls.py      - deterministic extraction walls, applied to every fact
ledger.py     - append-only facts with temporal validity
weighting.py / summary.py - selection and the rebuilt profile cache
db.py         - SQLite (WAL), snapshots, integrity checks
```

## The ledger is append-only

No automated path ever hard-deletes a fact. Supersession, quarantine,
and dismissal are reversible state changes; the single hard delete is
one API handler behind the owner credential, human-initiated by
definition. The same rule covers ingested messages and the access log,
so the history of what the system believed is always reconstructable.

## The transcript is ground truth; the summary is a cache

Ingested messages are never modified by any later pass. The profile
summary is rebuilt from valid facts, records which fact ids produced it,
and appends every regeneration to a restorable version history. A bad
summary is therefore detectable, traceable, and recoverable rather than
quietly authoritative.

## Writes are gated by shape, not by claimed identity

Only the literal `user` sentinel and registered `source_app`s write into
canon. An `mcp:*` origin is never trusted, even when it claims a
registered app, because identity over a local stdio transport is
self-asserted. Untrusted writes are stored and quarantined for review,
never rejected and never laundered.

## Every mined fact passes deterministic walls

Grounding, temporal grounding, and source trust quarantine a doubtful
fact (written, held, reviewable). System-meta and builder-process
chatter is dropped instead, because it is not biography and queueing it
floods review. Model politeness is not a defence; the walls are code.

## A fact's date is never invented

`event_date` is honoured only when the extractor names the single source
message and that message literally contains the calendar date. When the
text names no year, the year comes from the conversation, never from the
model. A fabricated date corrupts a timeline as surely as a fabricated
fact, so an ungrounded date falls back to conversation time.

## Selection is recency times importance; permanence is a tier

Facts decay on their event date with a half-life stretched by importance
(roughly four times longer at 9 than at 1). Durable and active pools are
selected separately so neither starves the other, and near-duplicates
collapse before selection so frequency cannot masquerade as salience.
Importance 10 never decays and only the owner can assign it; the miner
is hard-capped at 9.

## Open endpoints are defined by what they project

`/v1/recall` answers unauthenticated loopback callers because every chat
round calls it, so it returns exactly the six documented fields and at
most 50 rows. Everything that reads or writes exact rows (facts, review,
search, attachments, jobs) requires the owner credential even on
loopback. Browser sessions are opaque server-side ids in httpOnly
cookies, revocable at logout; the bearer token itself never rides in a
cookie.

## The word budget is enforced by rewriting, never truncation

An overlong summary draft gets one compress pass, and every failure mode
keeps the complete draft. Truncation would silently amputate the most
recent sections, which is the opposite of what a memory system owes you.
