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
human erasers on the admin surface (fact, file, message - the message
one is #45, closing crossband#106's loop): `test_message_erase.py`
proves the erase is owner-gated and single-row, search and health stay
honest afterwards, live facts mined from an erased message resurface
for review instead of vanishing, attachments are counted not cascaded,
and every eraser journals a content-free `erasures` row - what was
erased is gone, that it was erased is not.

**Person records (#33).** The wire identity (contract 1.2,
`test_identity_wire.py`): `speaker_identity` stores verbatim and absent
means 1.1 behaviour exactly; facts bind per the owner's policy
(introduced/owner-correction always, voice-match at 0.8+, weaker never);
merged slugs resolve to their winner, forgotten slugs bind nothing, and
binding never changes whether a fact is held. Admin surface and base: The admin surface renames (owner-set, clients
can't undo), merges (aliases, clips and fact links re-point; supersede
not rewrite; refused on forgotten sides), moves and deletes single clips
(moves collapse onto duplicates; deletes journal and unlink bytes) - all
owner-gated, all in `test_person_records.py`. And the slice-1 base: Owner-set names survive client updates; an
alias can never be reassigned to a different person; a model speaker
label can never become a person; clips are content-addressed and
owner-only on disk; sync includes forgotten marks; forget deletes the
audio (journalled content-free), moves the person's approved facts back
into review as one group and leaves held facts alone; every route
refuses callers without the owner token (`test_person_records.py`).

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
