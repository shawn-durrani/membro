# Architecture

Membro keeps every message it's sent, word for word, and never changes
one. In the background it mines each conversation for facts, and fixed
rules in the code check every fact before it's trusted. The profile is
rebuilt from the facts that pass. Apps reach it through an HTTP API
that answers only on your computer by default, and models through an
MCP server. Everything Membro remembers lives under `data/`, in one
SQLite database beside the files, voice clips and snapshots. The aim is
that you can always see what Membro believes and where each belief came
from. The decisions here are settled.

## The shape

```
api.py          the HTTP contract (docs/API.md), on loopback by default
mcp_server.py   four tools for models, and every save they make is held
mcp_admin_server.py  two read-only tools that need the owner's token
episodic.py     every message as it arrived, with full-text search
mining.py       reads a conversation and proposes facts
walls.py        the fixed checks every mined fact passes
ledger.py       the facts, each with the dates it holds for
recall.py       ranks facts for a question
weighting.py    picks which facts the profile is built from
summary.py      builds the profile and keeps every version
judge.py        the optional second look at held facts
persons.py      the people your apps recognise, and their voice clips
erasers.py      the owner's erasers for a fact, a file or a message
restore.py      puts a snapshot back and replays the erasures since
auth.py, sessions.py, passkeys.py  the admin page's sign-in
db.py           SQLite in WAL mode, snapshots, integrity checks
```

## Nothing automatic deletes a fact

The ledger, Membro's store of facts, is only ever added to. No
automatic path deletes a fact. A fact can be superseded, which means a
newer fact replaces it, or quarantined, which means it's held out of use
for review. It can also be dismissed after review. Each of those keeps
the fact. A held or dismissed fact can be approved again, and a
superseded one stays as history.

The only hard deletes are the owner's erasers and a restore. Each eraser
is one API handler behind the owner's credential, and a restore replays
those same erasures. Ingested messages and the access log follow the
same rule. A voice clip is the one exception, because an app that
captures voices can delete a clip its own voice bank has dropped, using
the owner's token.

## The messages are the truth, and the profile is a cache

Membro never changes a message it has stored. The profile is rebuilt
from the facts that are valid now. It records the ids of the facts that
produced it, and each rebuild is added to a version history you can
restore from. When you hold, erase or forget a fact the profile was
built from, the profile is rebuilt without it. An erase or a forget
does that straight away, and holds do it once, five minutes after the
last one.

## A write is trusted for where it comes from

Only two kinds of write go straight into the trusted facts. One is the
owner's own, marked with the literal origin `user`. The other names an
app listed in `trusted_apps`. An `mcp:*` origin is never trusted, even
when it names a trusted app. Over MCP's local connection a program can
claim to be anything, so the name proves nothing.

An untrusted write is still stored, and held for review. The MCP save
tool is the exception, because it drops chatter about Membro's own
workings or about building software. Any save shorter than 8 characters
or longer than 10,000 is refused.

## Every mined fact passes the walls

The walls are the fixed checks in the code that every mined fact
passes. Grounding checks that the fact's names appear in the chat it
came from. Temporal grounding does the same for its dates and times.
Source trust holds a fact from a chat that reads as roleplay, or from a
model's summary of you. A fact that fails one is quarantined for review. The
optional judge pass can release a fact held by grounding, but only when
it quotes the missing name from the chat word for word.

Talk about Membro's own workings, or about the work of building
software, is dropped instead. It isn't biography, and holding it would
flood the review queue.

## A mined fact's date is never invented

A mined fact keeps its `event_date`, the day the fact is about, only
when the miner names the one message it came from and that message
contains the date. A missing year comes from the conversation, never
from the model. A date that can't be traced falls back to the time of
the conversation. A fact you save directly keeps the date you send.

## Selection is recency times importance, and permanence is a tier

Facts fade from their event date, and the more important a fact is, the
slower it fades. A fact at importance 9 fades about 3.7 times slower
than one at 1. The profile draws on two pools, chosen separately. The
durable pool ranks facts by importance alone, and the active pool by
importance and how recent they are. Near duplicates collapse into one
before selection. A fact at importance 10 never fades, and only the
owner can give it that score. The miner can score up to 9.

## Open routes are defined by what they return

`/v1/recall` answers a caller on loopback, the computer's own address,
with no credential, because every chat round calls it. That's why it
returns only seven documented fields, and at most 50 rows.

Reading exact rows needs the owner's credential, even on loopback, and
so does changing or deleting a row that exists. That covers facts,
review, search, attachments, messages, person records, jobs and the
consolidation sweep. Sending messages and saving a new fact stay open.
A session is an opaque id the server keeps, sent in an httpOnly cookie,
and the bearer token never goes in a cookie.

The rule covers exact rows, and not everything that reveals something.
[SECURITY.md](SECURITY.md) names the open
routes that say more than you might expect, and
[docs/API.md](docs/API.md) gives the position of every route.

## The word budget is kept by rewriting

The profile has a word budget. A draft more than 20% over it gets one
pass that compresses it, and if that pass fails the complete draft is
kept. Membro never truncates a draft, because truncation would cut the
newest sections first. A draft under its floor, `memory_summary_fill`
of the budget, gets one pass that expands it from the same entries. That
pass runs only when the entries hold enough words to support it, so a
thin ledger keeps its short profile.

## A reply the model didn't finish is never saved

A cut-off draft keeps the profile you had, and a cut-off rewrite keeps
the draft it started from. The same holds for every model call. When a
miner reply is cut short or refused, the miner tries those messages
again in smaller pieces. A single message it still can't finish, or
that the model refuses, is left unmined. A binding retry that's cut
short counts as no answer, so its fact is held for review.
