# How memory works

The model in plain English, then what is capped and what is not. The cited
research is in [REFERENCES.md](REFERENCES.md), the wire contract in
[API.md](API.md). No real personal data appears here; examples are invented.

## Three layers, three jobs

1. **The tapes** (episodic record): every ingested message, word-for-word, with its
   real date and who said it. Never edited. Ground truth. Searchable verbatim.
   Files that travelled with messages (pasted documents, PDFs, images) are part
   of the tapes too: stored whole on disk, their text extracted where possible so
   search and the miner see it. A memory that silently dropped what you
   pasted wouldn't be ground truth.
2. **The cards** (the ledger): short durable facts distilled from the tapes, one
   fact per card. **Append-only**: a card is never thrown away; when something
   changes, the old card is flipped face-down (*superseded*) and kept as history.
3. **The profile** (the summary): one page built from the face-up cards, handed to the models at the start of every chat so they know you without a
   lookup. It is a *cache* over the cards, and the cards remain the source of
   truth.

A cheap miner model reads each conversation on the way out and writes cards.
The four extraction walls check each card before it is trusted.

Alongside the three layers sits **the access log**:
every deep recall, history search and summary fetch, from the chat app
and from the model-facing MCP tools alike, appends one row saying when it
happened and where the request came from. How much detail the row carries
depends on the kind: a recall records the question and which cards came
back, with their scores; a history search records the question and how
many messages matched (the results are tapes, not cards); a summary fetch
records only that the profile was read. It changes nothing about
what's remembered; it's how the system can later show you your memory
being *used* (the Mathematics page's live view reads it) and, eventually,
let often-recalled cards resist forgetting (reinforce-on-reuse).
Browsing the ledger *directly* (the admin page's Ledger table, or the
read-only admin MCP tools) is not logged today; the access log covers
lookups made on a model's behalf rather than your own inspection.

## What is capped, and what is not

"Cap" means several different things here and **only two of them are real
limits**: how much of one long message the miner reads, and how many cards
the profile folds.

### Storage: never capped
The ledger holds unlimited cards. Nothing is ever dropped, deleted, or aged out by
the software; a fact only ever becomes *superseded* or *quarantined* (both
reversible, both kept). The database growing is fine: invalid cards cost nothing
because they're simply not shown to the models.

### What the miner reads: bounded per message (a real limit)
The miner reads a *bounded view* of each message: the message plus
any text extracted from the files that travelled with it, truncated to
**20,000 characters** for extraction only. So a 200,000-character pasted
document contributes its first 20,000 characters to the cards; the tapes
still keep every byte, and a history search still finds any word of it,
including inside attached files. Long or long-idle conversations are mined
in windows of **120 messages / ~350,000 characters**, each window advancing a
watermark so an interrupted run resumes where it stopped instead of
re-reading (or overflowing) what it already mined. Both bounds exist because what the miner reads has to fit in one model call.

### Reading the ledger yourself (the admin UI): paged rather than capped
The ledger table fetches a page at a time (200 rows) for browser performance, then
offers **Load more**; the count reads "200+" and the table says that only the
first 200 are loaded and more may exist, so nothing is ever *silently* hidden.
Search scans the whole ledger rather than just the loaded page, but within the
status filter beside it (**valid** by default), and it returns the same
200-at-a-time window. Switch the filter to **all** to search quarantined and
superseded cards too.

### Recall (what a model gets when it looks something up): capped on purpose
A lookup returns the **top cards by relevance** (semantic + keyword + a bounded
recency weight), de-duplicated, never the whole ledger. The limit differs by
door: the MCP `recall_memory` tool asks for **up to 20** cards, while the HTTP
`POST /v1/recall` defaults to **10** and refuses more than 50. This is
deliberate: the model wants the few cards that bear on the question rather than
1,000 facts dumped into a tool result, so ranking plus a small limit is the
right behaviour here.

### The profile (always on): folds ~500 cards (a real limit)
The profile is rebuilt by *folding*: one pass in which the selected cards
are read together and rewritten into the one-page profile. The fold is fed
two pools of currently-valid cards: **up to 200 durable cards + up to 300
active ones** (pinned cards sit above the 200, outside the pools' slot
competition; see below).

Below ~500 facts, everything you have is represented: the two pools together
hold more cards than the ledger does, so nothing has to be left out. Past
that the fold hits its ceiling and cards start being left out of the
profile, out-selected on merit. So *which* cards make it matters. Selection (`weighting.py`, mechanisms cited
in [REFERENCES.md](REFERENCES.md)) is:

- **Recency × importance**, decaying on the card's true `event_date` (never
  insertion order, so a bulk-imported old card sorts as old), with the
  half-life stretched for important cards: a life-defining fact fades ~4×
  slower than a mundane one. Importance is LLM-scored 1–9 at extraction
  (10 is the owner-only permanence tier; see below); facts you save by
  hand start unscored and are selected as a neutral 5.
- **Two pools**: a durable pool (top cards by importance, age-blind:
  identity, family, history) and an active pool (ranked by how recent *and*
  how important, together: live threads), fed to the fold as separate
  labelled groups so neither can starve the other.
- **A permanence tier above both** (importance 10, owner-only): pinned cards
  never decay and sit in the profile unconditionally, outside the pools'
  slot competition. Over a years-long horizon every finite half-life reaches
  zero and any fixed-size shortlist can be crowded out, which is why
  permanence is a separate tier rather than a stretched half-life. The miner
  is capped at 9; only you can pin.
- **Paraphrase collapse first**: near-duplicate cards collapse to their best
  copy before selection, so how *often* something came up can't buy it more of
  the budget. The ledger keeps every copy; only the selection collapses.

The ledger still has every fact, and `recall_memory` can still fetch anything
on demand; a card missing from today's profile has merely been out-selected
and remains retrievable.

#### The word budget

The profile is written to a word
budget (default 2000, `memory_summary_words`) with a per-section ceiling, and
the budget is enforced by *rewriting*, never truncation: if the draft
overshoots by more than ~20%, it gets one "compress to budget" pass.
Truncation is never used because it silently amputates the last sections
(Goals and Recent Changes, the most current ones). Every failure mode falls
back to the complete-but-verbose draft, so a failed rewrite can leave the
profile over budget but never incomplete, and the admin page shows the
actual word count next to the budget so you can see the promise being
kept. **Rebuilds are never destructive**: every generated profile is kept in
an append-only version history, and any earlier version can be restored from
the admin page if a rebuild reads worse than what it replaced; this is the
same never-delete standard the ledger holds facts to.

#### Topics morph with your life

The spine is fixed (Identity,
Preferences, Relationships & People at the stable top; Goals & Active
Threads, Recent Changes at the volatile bottom) because that stable→volatile
separation is the load-bearing structure. But the middle sections are named
by the model from what your facts actually cluster around: a hobby, a
project, a career thread. Topics appear when they earn their space and
dissolve as their facts fade; the forgetting curve becomes visible in the
document's own table of contents. (Research lineage and the caveats are in [REFERENCES.md](REFERENCES.md). This began behind a config flag as a
reversible experiment and later became the only layout; there is no
fixed-headings mode to switch back to.)

The summary is also the one
job routed to a stronger model
(`summary_model`, default Sonnet): it's the most-read artefact in the system
(every model, every round) while rebuilds are rare, so the judgement of what
deserves space is worth paying for; the cheap miner keeps the high-volume
extraction work.

## What is planned

Per-section fold budgets, half-life tuning on real data, incremental rebuild,
and consolidation v2 (clustered merge proposals you approve). The research
behind each is in [REFERENCES.md](REFERENCES.md); the work itself is tracked
in the public issues.

Underneath all of it, one principle: **the ledger is the system of record and
the profile is a fast cache over it.** A refinement makes the cache smarter
without ever making it the source of truth, so a two-year-old stable fact
stays recallable even when it is not in today's profile.

## Why SQLite

The choice is deliberate. This is a single human's memory on their own machine:
SQLite means no server to install or babysit (adoptability), backups and
restores are consistent file copies (the entire safety story), FTS5 is built
in (verbatim search over tapes and attachments), and WAL handles the real
concurrency pattern here: one writer, several readers, occasional MCP-process
writes. Headroom is measured in millions of rows; the first real boundary is
vector search past ~100k facts, which is an index addition (`sqlite-vec`)
rather than a database swap. And because clients only ever speak the versioned
HTTP contract, never the database itself, the choice is an implementation
detail that could be replaced without breaking anyone.

## Current limitations
- Summary selection is salience-weighted but not yet section-budgeted (the word
  budget has per-section caps; the *fold selection* doesn't yet); the decay
  half-life is untuned.
- Embeddings need an OpenAI key; without one, recall degrades to keyword-only.
- Recall ranks but doesn't *resolve* contradictions: a stale card is
  out-ranked, never auto-retired; retiring it needs a human supersede (or the
  consolidation sweep, which today proposes exact-duplicate supersessions and
  pin nominations for you to approve; clustered merges are Consolidation v2,
  still to come).
