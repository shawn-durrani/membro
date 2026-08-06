# What the test suite guarantees

The suite is the enforcement layer for the promises in
[ARCHITECTURE.md](../ARCHITECTURE.md) and [SECURITY.md](../SECURITY.md).
It runs keyless by design: with no API keys configured, every test
passes, because keyless degradation is itself one of the promises. Run
it with:

```sh
.venv/bin/python -m pytest -q
```

## The guarantees, grouped

**Append-only integrity.** No source path issues a delete or update on
facts, messages, attachments, or the access log; the tests grep the
source for forbidden statements as well as exercising behaviour.
Supersession, quarantine, and dismissal round-trip reversibly; summary
regenerations append to a restorable version history.

**Extraction walls.** Ungrounded names quarantine; ungrounded or
relative dates never mint an event date (the year can only come from the
conversation); untrusted sources quarantine rather than entering canon;
system-meta and builder-process chatter is dropped without flooding the
review queue, while a genuine biographical fact in the same conversation
survives. Overlapping distills for one conversation cannot double-mine.

**Auth.** Every exact-row surface (facts, review, verbatim search,
attachments, jobs, consolidate) refuses an unauthenticated caller even
on loopback, and the wrong token is refused, not just a missing one.
Unauthenticated pages never contain a credential. Sessions are opaque
ids: provably not the bearer token, expiring, revoked everywhere by
logout, and immune to fixation. Non-loopback hosts reach the lock screen
and nothing else unless trusted and signed in.

**The recall boundary.** `/v1/recall` returns exactly the documented
fields and at most 50 rows; the projection test fails if a field is
added without amending the contract.

**The contract.** `tests/test_api_contract.py` drives the full HTTP
surface as a client would; a reimplementation or a stub that passes it
speaks the same contract.

**Selection and the summary.** Half-life stretches with importance;
permanence is owner-only (the miner cannot mint a 10); near-duplicates
collapse before selection; the word budget is enforced by rewriting with
every failure mode keeping the complete draft; provenance tags derive
mechanically and the summary prompt is pinned against misattribution.

**Operations.** Snapshots are consistent, rotated, change-detected, and
mirrored best-effort; the launchd plist template contains no
machine-local values and cannot collide with a sibling service's port;
the leak scanner rejects real-shaped secrets, identifiers, and
deny-listed personal content, passes documented placeholders, and the
committed tree must scan clean (a test also proves the tree walk can
detect a planted leak, so the gate cannot rot silently).

**Process docs.** CONTRIBUTING.md and CLAUDE.md are asserted to agree on
how work lands, because they once drifted and the stale one was the
guide a contributor would follow.

## What the suite deliberately does not cover

Extraction quality (whether the miner proposes good facts from real
conversation) is judged by benchmarks and by use, not asserted in unit
tests; green CI means the machinery keeps its promises, not that the
model is wise. There is no load or concurrency testing beyond the
distill lock, and no network fuzzing: the service is loopback-first by
design.
