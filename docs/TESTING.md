# What the test suite guarantees

The suite holds the promises in [ARCHITECTURE.md](../ARCHITECTURE.md)
and [SECURITY.md](../SECURITY.md). It runs keyless, so with no API keys
every test passes or skips. Run it with this command.

```sh
.venv/bin/python -m pytest -q
```

## Nothing is lost by accident

Only the owner's erasers delete a fact or a message. Nothing rewrites a
message, an attachment, a caption, a row in the access log or a stored
profile version. A change to a fact's state updates its row, and keeps
it. Tests read the source for forbidden statements on attachments,
captions, the access log and profile versions, and check facts and
messages by what they do. Superseding, quarantining and dismissing a
fact all keep it, and a held or dismissed fact can be approved back.
Every profile rebuild adds to a history you can restore from.

Content is deleted only by the owner's erasers, for a fact, a file and
a message, and by the People page's clip delete and Forget. A capture
app can also drop a clip from its own voice bank. `test_message_erase.py`
pins the message eraser. The erase needs the owner's credential and
takes one row. Search and health stay accurate afterwards. Live facts
bound to an erased message come back for review, and don't vanish. Its
attachments are counted, and never deleted with it.

An erased image leaves no caption behind, in its table or in the search
index (`test_captions.py`). No search can match it, or show its
caption's words again (`test_search_captions.py`). Every eraser writes a
row to the `erasures` journal that holds no content. What was erased is
gone, and the record that it was erased stays.

## Person records

Names the owner set survive updates from apps. An alias can never move
to a different person, and a model's speaker label can never become a
person. Clips are stored by their content and kept owner only on disk,
and a sync includes the marks for forgotten people. Forget deletes the
audio and journals it without content. It moves the person's approved
facts back into review as one group, and leaves held facts alone.

The admin surface is pinned the same way, and every route on it needs
the owner's credential. A rename is set by the owner, and apps can't
undo it. A merge re-points aliases, clips and fact links. It supersedes
and never rewrites, and it's refused when either side is forgotten.
Moving a clip collapses it onto a duplicate, and deleting one journals
the erasure and unlinks the bytes. Every route refuses a caller without
the owner token (`test_person_records.py`).

## Clips an app has stopped using

A capture app can send the list of clips it keeps for each person,
which contract 1.8 added. The list is stored, and deletes nothing on its own. The
count of unused clips leaves out clips stored after the list, and drops
to zero when the person changes after it. The owner's delete, for one
person or for everyone, removes the rows and the files nothing else
shares, and journals one row per clip. An app's own drop is an ordinary
clip delete that journals its reason (`test_contract_1_8.py`).

## Restoring a snapshot

A restore replays every erasure made since the snapshot, so nothing you
erased comes back. That covers facts, files, messages, deleted clips and
forgotten people. A clip moved or merged to someone else and then
deleted is found under its old owner. Bytes another clip still uses are
kept. A forgotten person comes back forgotten, with no clips, no lists
of kept clips, and their approved facts in review. A clip whose audio
file is missing is listed and kept. Everything else reads the same as
the snapshot, and the journal comes out whole (`test_restore.py`,
`test_restore_voice.py`).

## Who spoke

An app can say who it believes spoke a message, in `speaker_identity`,
which contract 1.2 added. It's stored word for word, and a message without it
behaves as it did under 1.1. Facts bind to a person by the owner's
policy: an introduction and an owner's correction always, a voice match
at 0.8 or more, and anything weaker never. A merged person's name
resolves to the person it merged into, and a forgotten one binds
nothing. Binding never changes whether a fact is held
(`test_identity_wire.py`).

## The extraction walls

A fact with names its chat never mentions is quarantined. A date the
chat never stated, or a relative one, never becomes an event date, and
a missing year comes from the conversation. A fact from a source Membro
doesn't trust is quarantined. Chatter about the memory system, or about
building software, is dropped without flooding review, and a fact about
you in the same conversation survives. Two distills that overlap can't
mine a conversation twice.

## Signing in

Every surface that returns exact rows refuses a caller with no
credential, even on loopback. That's facts, review, word for word
search, attachments, jobs and the consolidation sweep. A wrong token is
refused, as well as a missing one. A page shown before sign-in never
contains a credential. A session is an opaque id, and never the bearer
token. It expires, logging out revokes it everywhere, and nobody can
plant a session id in advance. A host off loopback is refused unless
it sends the bearer token. A trusted host also reaches the lock screen,
and the rest once a browser has signed in.

## The recall boundary

`/v1/recall` returns only the documented fields, and at most 50 rows.
Adding a field fails the projection test until the contract changes to
include it.

## The contract

`tests/test_api_contract.py` drives the core client routes the way a
client would. The rest of the surface has its own test files, like the
person routes, attachments, messages, profile versions, backups, the
busy route and the viz routes.

## Choosing facts for the profile

A card fades slower the more important it is. Only the owner can make a
card permanent, and the miner can't score a 10. Near duplicates collapse
before selection. The word budget is kept by rewriting, and every
failure keeps the complete draft. A short draft is expanded once from
its entries, only when they hold the material, and nothing is made up.
The provenance tags are worked out mechanically.

## Operations

Snapshots are rotated, and taken only when the database has changed.
Their consistency comes from SQLite's own backup API, and no test checks
it. The launchd plist template holds nothing specific to one machine.

The leak scanner rejects secrets with a real shape, identifiers of a real
machine or person, and anything on the local deny list. It lets through
the placeholders the docs use, and the committed tree must scan clean. A
test plants a leak in a temporary tree, to prove the tree walk can find
one.

The busy route names each kind of work in flight by a fixed label, and
answers without a credential. A mark older than an hour stops counting.

## The docs

Tests check that CONTRIBUTING.md and CLAUDE.md agree on how work lands.
`tests/test_doc_style.py` holds every markdown file to the writing
ceilings, and each doc in its `CONVERTED` list to the writing rules in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## The benchmark harness

`tests/test_bench_memory_*.py` pin the guards that keep a benchmark run
off your real apps, without starting any service. The harness and the
service read the disposable marker the same way, on every hostile input.
A throwaway app's environment holds only what the harness names. A
run's folder is deleted only while the run's root still proves it's
the run's own. The meter
reads and prices each provider's replies, stops at the budget, and
speaks both SDKs' wire formats in mock mode.

The grading prompts are pinned by a hash on every run. They match the
upstream scorer byte for byte, and a test checks that when
`LONGMEMEVAL_SRC` points at a checkout of the upstream code.

## What isn't covered

Extraction quality is judged by benchmarks and real use, and unit tests
don't measure it. The benchmark is LongMemEval, run by
[bench_memory](../bench_memory/README.md). There's no load testing
beyond the distill lock, and no network fuzzing, because the service
answers on loopback first.
