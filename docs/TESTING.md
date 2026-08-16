# What the test suite guarantees

The suite enforces the promises in [ARCHITECTURE.md](../ARCHITECTURE.md)
and [SECURITY.md](../SECURITY.md). It runs keyless: with no API keys,
every test passes. Run it with:

```sh
.venv/bin/python -m pytest -q
```

## Guarantees

**Append-only integrity.** No source path deletes or updates facts,
messages, attachments, or the access log; tests grep the source for
forbidden statements as well as exercising behaviour. Supersession,
quarantine, and dismissal round-trip reversibly. Summary regenerations
append to a restorable history. The only deletes anywhere are the three
human erasers on the admin surface: fact, file, and message.
`test_message_erase.py` pins the message one. The erase is owner-gated
and single-row; search and health stay accurate afterwards; live facts
mined from an erased message resurface for review instead of vanishing;
attachments are counted, not cascaded. Every eraser journals a
content-free `erasures` row, so what was erased is gone and that it was
erased is not.

**Person records.** Owner-set names survive client updates. An alias can
never be reassigned to a different person, and a model speaker label can
never become a person. Clips are content-addressed and owner-only on disk,
and sync includes forgotten marks. Forget deletes the audio (journalled
content-free), moves the person's approved facts back into review as one
group, and leaves held facts alone.

The admin surface is pinned the same way, all of it owner-gated. A rename is
owner-set and clients cannot undo it. A merge re-points aliases, clips and
fact links, supersedes rather than rewrites, and is refused when either side
is forgotten. Moving a clip collapses it onto a duplicate; deleting one
journals the erasure and unlinks the bytes. Every route refuses callers
without the owner token (`test_person_records.py`).

**Wire identity (contract 1.2).** `speaker_identity` stores verbatim, and an
absent field means 1.1 behaviour exactly. Facts bind per the owner's policy:
introduced and owner-correction always, voice-match at 0.8+, weaker never.
Merged slugs resolve to their winner, forgotten slugs bind nothing, and
binding never changes whether a fact is held (`test_identity_wire.py`).

**Extraction walls.** Ungrounded names quarantine. Ungrounded or
relative dates never mint an event date; a missing year comes from the
conversation. Untrusted sources quarantine. System-meta and
builder-process chatter is dropped without flooding review, while a
biographical fact in the same conversation survives. Overlapping
distills cannot double-mine a conversation.

**Auth.** Every exact-row surface (facts, review, verbatim search,
attachments, jobs, consolidate) refuses unauthenticated callers even on
loopback; a wrong token is refused, not just a missing one.
Unauthenticated pages never contain a credential. Sessions are opaque
ids: not the bearer token, expiring, revoked everywhere by logout,
immune to fixation. Non-loopback hosts reach only the lock screen
unless trusted and signed in.

**The recall boundary.** `/v1/recall` returns exactly the documented
fields, at most 50 rows. Adding a field fails the projection test until
the contract is amended.

**The contract.** `tests/test_api_contract.py` drives the full HTTP
surface as a client would.

**Selection and summary.** Half-life stretches with importance;
permanence is owner-only (the miner cannot mint a 10); near-duplicates
collapse before selection; the word budget is enforced by rewriting,
with every failure mode keeping the complete draft; provenance tags
derive mechanically.

**Operations.** Snapshots are consistent, rotated, and change-detected;
the launchd plist template contains no machine-local values; the leak
scanner rejects real-shaped secrets, identifiers, and deny-listed
content, passes documented placeholders, and the committed tree must
scan clean. A test plants a leak in a temp tree to prove the tree walk
can detect one.

**Process docs.** CONTRIBUTING.md and CLAUDE.md are asserted to agree
on how work lands; they drifted once.

## Not covered

Extraction quality is judged by benchmarks and use, not unit tests.
There is no load testing beyond the distill lock and no network
fuzzing; the service is loopback-first.
