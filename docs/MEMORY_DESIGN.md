# How memory works

Membro's memory has three layers, each with its own job, and a small
model that turns one into the next. The real limits sit in only a few
places, and the rest of the design makes sure nothing it learns is lost.
The research behind it is in [REFERENCES.md](REFERENCES.md), and the
wire contract in [API.md](API.md). Every example is made up.

## Three layers, three jobs

**The tapes** are the record of every message Membro has been sent,
word for word, with its real date and who said it. Nothing automatic
ever edits them, and they're the ground truth. You can search them word
for word. Files that came with a message, like a pasted document, a PDF
or an image, are part of the tapes too. They're stored whole on disk,
and their text is pulled out where possible, so search and the miner see
it. A memory that quietly dropped what you pasted wouldn't be ground
truth. You can erase a message or a file by hand.

**The cards** are the ledger: short, lasting facts drawn from the tapes,
one fact to a card. Nothing automatic throws a card away. When something
changes, the old card is turned face down and kept as history, which
the code calls superseded. You can edit a card's wording, or erase it,
by hand.

**The profile** is one page built from the face-up cards. The models
get it every round, so they know you without looking anything up. A
card bound to one chat, such as one drawn from a guest's words, never
goes into it, because every chat reads the profile. The profile is a
cache over the cards, and the cards stay the source of truth.

A cheap model, the miner, reads each conversation after it happens and
writes cards. The four extraction walls check each card before it's
trusted. [MEMORY_INTEGRITY.md](MEMORY_INTEGRITY.md) says what they are.

## The access log

Beside the three layers sits the access log. Every deep recall, history
search and profile fetch adds one row saying when it happened and where
the request came from. That holds for the chat app and for the MCP tools
alike. A recall records the question and which cards came back, with
their scores. A history search records the question and how many results
came back, which is at most the search's limit. A profile fetch records
only that the profile was read.

The log changes nothing about what's remembered. It's how Membro shows
you your memory being used, on the Mathematics page's live view. One day
it may let the cards you recall often resist fading, an idea called
reinforce on reuse. Browsing the Ledger table on the admin page isn't
logged, and neither is using the read-only admin MCP tools. Viewing the
profile on the admin page is logged as a profile fetch, and a history
search you run yourself is logged too.

## What the miner keeps

The miner keeps facts about you that should still hold in six months or
so. That's who you are and your age, your work and projects, what you like,
what you've decided and what you're working towards. It's the people in
your life, down to how many brothers and sisters you have. It's also what
you own and use, what you've done, and your home and pets.

Most of that comes up in passing. You mention your car while asking about
tyres, or your sister while planning a gift. The miner keeps the aside even
when the rest of the chat is a one-off task. It judges each chat on its
own. It's shown your cards only so it won't repeat one and can update one.

It leaves out passing moods, chit-chat and the AI's own suggestions. It
also leaves out the details of a one-off task, like what one purchase cost
or how many shoes you packed for a trip. When a lasting fact comes with a
one-off number, it keeps the fact and drops the number. In a chat about
building something, it keeps at most one line per project, about what
you're working on or what shipped, and never the technical detail. It
never keeps facts about the AIs or about Membro itself, a model's summary
of what it knows about you, biography from roleplay, or anything the
ledger already holds.

## What has a real limit

The real limits are on how much of one long message the miner reads, how
much of the ledger it's shown, and how many cards go into the profile.
Storage has no limit. The admin page pages through the ledger, and
recall keeps its answers short by choice.

### Storage has no limit

The ledger holds as many cards as you have. The software never drops,
deletes or ages out a card. A card only ever becomes superseded or
quarantined, which means held for review, and both kinds are kept. A
quarantined card can be approved again. A growing database is fine. A
card that isn't valid costs nothing, because recall stops returning it
at once, and the profile leaves it out from its next rebuild.

### The miner reads part of each message

The miner reads each message plus any text pulled from its files, cut to
20,000 characters, and only for mining. A 200,000 character pasted
document gives the cards its first 20,000 characters. The tapes still
keep every byte, and a history search still finds any word of it,
inside attached files too.

A long conversation, or one that sat idle a long time, is mined in
windows of 120 messages or 350,000 characters, whichever comes first.
Each window moves a watermark forward, so an interrupted run picks up
where it stopped, and never reads again or overflows on what it already
mined. Both limits exist because what the miner reads has to fit in one
model call.

### The miner sees part of the ledger

The miner sees your existing cards, so it won't repeat one and can mark a
card that a new fact updates. It's shown the cards a recall from that chat
would find. Those are the ones every chat can see, and the ones bound to
this chat.
A card bound to another chat, such as one drawn from a guest's words there,
never reaches it.

It gets at least the 250 newest valid cards every chat can see, oldest
first, plus this chat's own cards from the same start. It also gets up
to 40 older cards that match the chat's topic. The newest list starts
from a card number that moves in steps of 32, so it can hold up to 31
more than 250. That keeps its start the same from one call to the next,
so a burst of mining calls can share a prompt cache. Only the cards
every chat can see decide that start, so every chat's list begins at
the same card.

### The admin page pages through the ledger

The Ledger table on the admin page loads 200 rows at a time, so the
browser stays quick, and offers **Load more**. The count reads "200+",
and the table says only the first 200 are loaded and more may exist, so
nothing is hidden without a word. Load more reaches 1,000 rows. Past
that the request is refused and the table shows an error, so narrow it
with search.

Search covers the whole ledger, within the status filter beside it,
which is **valid** by default. It returns the same 200 row window.
Switch the filter to **all** to search quarantined and superseded cards
too.

### Recall returns the few cards that fit

A lookup returns the top cards by relevance, with duplicates removed,
and never the whole ledger. Relevance mixes meaning, matching words, and
a small boost for newer cards. The limit depends on how you ask. The MCP
`recall_memory` tool asks for up to 20 cards, and HTTP `POST /v1/recall`
defaults to 10 and refuses more than 50. A model wants the few cards
that bear on its question, and a thousand facts dumped into a tool
result would bury them. Ranking plus a small limit is the right
behaviour here.

## The profile folds about 500 cards

The profile is rebuilt by folding. That's one pass that reads the
selected cards together and rewrites them into the one-page profile. The
fold gets two pools of cards that are valid now: up to 200 durable cards
and up to 300 active ones. Pinned cards come on top of the 200, outside
the pools' competition for places.

Under about 500 facts, everything you have is in the profile, because
the two pools together hold more cards than the ledger does. Past that
the fold is full, and cards start being left out on merit. Which cards
make it then matters. `weighting.py` chooses them, and
[REFERENCES.md](REFERENCES.md) cites the research behind each step.

- Recency times importance. A card fades from its true `event_date`,
  never from when it was inserted, so a card from an import of old
  chats sorts as old. The more important a card, the slower it fades,
  and a life-defining fact fades about 3.7 times slower than a mundane
  one. The miner scores importance from 1 to 9 as it extracts, and 10
  is the owner's permanence tier. Facts you save by hand start unscored
  and count as 5.
- Two pools. The durable pool holds the most important cards whatever
  their age: identity, family, history. Among cards of equal
  importance, the newest come first. The active pool ranks cards by how
  recent and how important they are together, which finds the threads
  that are live now. The two go to the fold as separate labelled
  groups, so neither can crowd out the other.
- A permanence tier, outside both pools. A pinned card, at importance 10, never
  fades and always sits in the profile, outside the pools' competition.
  Over years every finite fade reaches zero, and any fixed shortlist
  can be crowded out. That's why permanence is a tier of its own, and
  not a slower fade. The miner can score up to 9, and only you can pin.
- Near duplicates collapse while the pools fill. The durable pool takes
  the 250 most important cards, collapses near duplicates to the
  strongest copy, and keeps 200. The active pool collapses its top 375
  against the durable pool. How often something came up can't buy it
  more of the budget. Paraphrases are caught only with embeddings, at a
  similarity over 0.93. Without them, only cards with the same words,
  ignoring case and punctuation, collapse. The ledger keeps every copy,
  and only the selection collapses.

The ledger still has every fact. A card missing from today's profile has
only been out-selected, and `recall_memory` can still fetch it when
asked. A card bound to one chat never joins the profile. It comes back
only to an HTTP recall that names that chat. `recall_memory` names no
chat, so it never returns one.

### The word budget

The profile is written to a word budget, 2,000 words by default
(`memory_summary_words`), with a ceiling for each section. The budget
is kept by rewriting, never by truncating. A draft more than 20% over
the budget gets one pass that compresses it.

The budget is also a target. The prompt asks for a range, from
`memory_summary_fill` of the budget, 80% by default, up to the budget,
spent on the specifics the entries carry. A draft under that floor gets
one pass that expands it from the entries. That pass runs only when the
selected entries hold at least twice the floor in words, and a draft
still short after it is kept. Nothing is made up to fill the room.

Membro never truncates, because truncation would quietly cut the last
sections, Goals and Recent Changes, and those are the most current. Every
failure falls back to the complete draft, however long. A failed rewrite
can leave the profile over budget, but never incomplete. The admin page
shows the word count next to the budget, so you can check it.

The writer checks how each reply ended. A draft the model didn't finish,
because it ran out of room, is never saved. The profile you had stays,
and the service log says why. A rewrite that stops short is thrown away
the same way, and the draft it started from stays.

Rebuilds never destroy anything. Every profile Membro builds is kept in
a version history that's only added to. If a rebuild reads worse than
the one it replaced, restore any earlier version from the admin page.
That's the same never-delete rule the ledger keeps for facts.

### Topics change with your life

The profile's spine is fixed. Identity, Preferences, and Relationships &
People sit at the stable top. Goals & Active Threads and Recent Changes
sit at the changing bottom. That split, from stable to changing, is the
structure the rest hangs on.

The middle sections are named by the model, from what your facts
cluster around: a hobby, a project, a career. A topic appears when it
earns the room and fades as its facts fade, so the table of contents
shows your memory's forgetting curve. The research behind it, and its
caveats, are in [REFERENCES.md](REFERENCES.md). There's one layout, and
no fixed-headings mode to switch back to.

The profile is the one job sent to a stronger model by default, set in
`summary_model`, which is Sonnet out of the box. It's the most read
thing in the system, by every model in every round, and rebuilds are
rare. The judgement about what deserves room is worth paying for there.
The cheap miner keeps the high-volume extraction work.

## What's planned

The plans are budgets for each section of the fold, and tuning how fast
cards fade on real data. Rebuilding only what changed is planned too,
and so is a second version of the consolidation sweep, which proposes
merging clusters for you to approve. The research behind each is in [REFERENCES.md](REFERENCES.md),
and the public issues track the work.

Each of these makes the profile smarter, and none makes it the source of
truth. The ledger stays the record, so a stable fact from two years ago
can still be recalled when today's profile leaves it out.

## Why SQLite

[SQLite](https://sqlite.org) is a database that lives in one file and
needs no server. Membro holds one person's memory on their own computer,
and SQLite fits that.

- There's no server to install or look after.
- A backup is a consistent copy of the file. A restore copies one back
  and replays the erasure journal, so what you erased stays erased.
- Full-text search is built in, as FTS5, and it drives word for word
  search over the tapes and attachments.
- Its write-ahead log handles the real pattern here: one writer, several
  readers, and now and then a write from the MCP process.

The headroom is millions of rows. The first real limit is vector search
past about 100,000 facts, and that's an index added on, `sqlite-vec`,
without changing database. Client apps speak only the versioned HTTP
contract, never the database, so the database could be swapped without
breaking them. Membro's own MCP server is the one exception, because it
opens the database file directly.

## Current limitations

- The word budget has a ceiling for each section, but the fold's choice
  of cards doesn't yet. How fast cards fade hasn't been tuned on real
  data.
- Embeddings need an OpenAI key, or a local server named in
  `embedding_base_url`. Without either, recall matches keywords only,
  and only exact duplicates collapse.
- Recall ranks, and doesn't resolve contradictions. A stale card is
  outranked, and never retired automatically. Retiring it takes a human
  supersede, or the consolidation sweep. The sweep proposes exact
  duplicate supersessions and cards to pin, for you to approve. Merging
  clusters is the second version of the sweep, still to come.
