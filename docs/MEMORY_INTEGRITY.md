# Memory integrity: why the walls exist

Most DIY AI memory is a markdown file that only grows. The problem isn't storage;
it's that a wrong line, once written, sits there forever and quietly spreads. This
document is the *why* behind this project's defences: a worked example of how memory
gets poisoned, and the four walls that stop it. (The example below is **synthetic**,
with invented names and facts, but the failure mode is real and is what these walls
were built against.)

## The contamination cascade (a worked example)

Meet "Sam." Here's a three-step poisoning, none of it done on purpose by the user:

1. **A roleplay seeds a fiction.** Sam opens a mock-interview session: *"Pretend I'm a
   candidate based in Metropolis, walk me through the questions."* The word
   *Metropolis* is now in a chat, as a stage direction rather than a fact about Sam.
   Sam actually lives in Riverdale, and has said so plainly in nine other chats.

2. **The model launders the fiction into a claim.** Later Sam asks a model *"what do
   you know about me?"* The model, summarising loosely, writes *"Sam is based in
   Metropolis."* The note-taker mines that assistant summary and files a card:
   *Sam lives in Metropolis.*

3. **The lie multiplies.** The note-taker is shown the current ledger (it has to be;
   that's how it de-dupes and detects supersessions). Now *Metropolis* looks
   canonical, so it gets restated and reinforced, stamped onto home-renovation chats,
   scheduling chats, chats that never mentioned any city at all.

Ground truth: **zero** user messages ever said Sam lives in Metropolis. One roleplay
stage-direction became, through the model's own manners, a "fact" that spread across
the whole ledger. A markdown-file memory has no defence against this. It's the exact
class of bug that motivated everything below.

## The principle: constrain the write path

The bug isn't that the model is malicious; it's that a *soft instruction* ("here's
the ledger, don't repeat entries") gets treated as a fact source, and LLMs don't
partition context cleanly. You cannot fix that by asking the model more nicely. You
fix it with **deterministic checks on the write path** that don't care how polite the
model was. Hence four walls, applied to every mined fact.

## The walls

A mined fact that trips the **grounding**, **temporal-grounding** or
**source-trust** wall is *written but quarantined* (held out of recall and the
summary, flagged low-confidence), never silently trusted. A human clears the queue.

1. **Grounding**: the fact's proper nouns must actually appear in its own source
   chat. *"Sam lives in Metropolis"* mined from a chat where "Metropolis" never
   appears → quarantined. This catches the *spread* (step 3).

2. **Temporal grounding**: the same rule, for *time*. A fact may not assert a
   calendar date or a relative schedule its source never stated. Wall 1 checks proper
   nouns, so it never looked at dates: *"the appointment is Saturday (tomorrow from
   the conversation date)"*, mined from *"on Saturday, roughly nine days away"*, kept
   the real weekday, invented the interval, and passed every wall at high confidence;
   the result was broadly-correct biography with wrong near-term planning. Dates
   compare *semantically*, so "June 30" in the source grounds "2026-06-30" in the
   fact. A fact that keeps the source's own qualifier verbatim always passes, which
   is the behaviour this steers toward: the reader resolves the phrase against the
   fact's date instead of trusting a resolution the service guessed at write time.

3. **Source-trust**: biography is not mined from roleplay / persona / interview-prep
   framing, or from an assistant's own "what do you know about me" dossier. This
   catches the *seed* (steps 1–2) even when the proper noun *is* present in the source.

4. **System-meta (a drop rather than a quarantine)**: a "fact" about this memory
   system's own machinery or the AI tooling itself ("the memory ledger quarantined
   100 facts", "the grounding wall over-flagged", "PID 4321") is not biography about
   the user at all, so there is nothing to review; quarantining it only floods the
   queue when a conversation happens to be *about the memory system*, a failure mode
   this project hit in practice. Such lines are never extracted. The filter is
   deliberately narrow: it matches system/AI-tooling *mechanics* vocabulary only,
   never generic verbs ("deployed", "committed") or product names a real
   career/project fact might carry; those stay eligible and are judged by the three
   quarantine walls, so at worst they land in review, never silently dropped. The
   verbatim message always survives in the episodic record, so nothing is lost.

## Why the note-taker still sees the ledger

It would be tempting to "fix" contamination by hiding the ledger from the note-taker.
That breaks de-duplication and supersession detection: the miner could no longer tell
"already known" from "new," or "this updates that." The ledger stays visible; the
walls make the *write* safe instead. This trade-off is deliberate.

## What's guaranteed, and what isn't yet

**Guaranteed today (enforced in code + tests; see `walls.py`, `mining.py`,
`test_walls.py`, `test_mining.py`, `test_meta_conversation.py`,
`test_invariants.py`):**
- Append-only: no automated path deletes a fact (supersede / quarantine / dismiss
  only; the sole hard delete is a human pressing the button).
- The episodic transcript is ground truth and is never modified by any pass, which is
  what makes a bad card *detectable* and traceable to its source.
- External writes (any MCP client) are quarantined at creation regardless of what
  they claim to be; the same "constrain the write" principle applied to authorship.
- A mined fact's **event date** (the date the fact is *about*) is only honoured when
  the extractor names the single message it drew the fact from and an explicit
  calendar date for it appears in *that* message's text. A date the model invented,
  or one that merely sits elsewhere in the same chat, is rejected outright and the
  fact is dated to the conversation instead. If the anchoring phrase names no year
  ("June 30"), the year comes from the conversation, never from the model, so no
  part of the date is guessed. This is the same "constrain the write" rule applied
  to time: a fabricated date corrupts a timeline as quietly as a fabricated city
  corrupts a biography.
- The same rule now covers the card's **wording**, not just its date field: a card
  asserting a calendar date or a relative schedule ("tomorrow", "in nine days",
  "next Saturday") that its source never stated is quarantined for review. Before
  this, only proper nouns were checked, so a fact could carry a resolved date
  nobody wrote and still read as high-confidence.
- **A card's source turn is recorded only when it is real.** A mined card
  points at one message only when the extractor actually named that message,
  and when the extractor has to be re-asked for a binding it omitted, the turn
  it names must share the card's own wording — and must be at least as
  plausible a source as any other speaker's turn in the same window. A card
  nothing could be tied to is stored *unbound*, and the review queue says so,
  rather than being attributed to whichever message happened to end the mining
  window. Wrong provenance is quieter than a wrong fact and harder to unpick:
  it makes a guest's sentence, or a synthesis of several turns, read like
  something the owner said.
- **A re-mine can't duplicate a card that's still current.** Two mining passes over
  one conversation can't overlap (a per-conversation lock), and a pass that writes
  facts and then dies before recording how far it read adds nothing on the retry: an
  identical, still-current card from that same conversation collapses onto the row
  already there. Duplicates aren't just clutter: five copies of one wrong card read
  like five confirmations. The match is on exact wording, which is what that
  crash-retry case needs; a reworded re-mine is a different problem (see below).

**Supersession direction is enforced.** The miner may propose that a new
fact retires a listed one, but two guards bound what a proposal can actually do.
A fact whose grounded event date is more than a day older than its target's
cannot supersede it, so mining imported or re-mined history files old claims
as *dated history* rather than as replacements for newer truth. And a fact the
write gate held for review cannot alter canon at all: its replacement intent is
deferred, and today surfaces as a count in the distill result, for the human
pass to apply or ignore. Both guards bind the automated path only; the owner's
supersede action in review stays unrestricted.

**Honest limitations (tracked in the public issues):**
- The walls are high-recall *detection* rather than perfect prevention: they flag
  for review (~12% false-positive on genuinely valid facts in the original tuning);
  they don't hard-reject. A stricter semantic gate is planned.
- Recall *ranks* current facts above stale ones but doesn't *resolve* contradictions;
  retiring a stale fact needs a human supersede or the future consolidation pass.
- A deferred supersession proposal (one made by a held-for-review fact) is reported
  only as a count in the distill result; it is not yet carried per-fact into the
  review queue, so the reviewer applies or ignores the replacement intent by hand.
- The re-mine guard above matches **exact wording** within one conversation: a pass
  that paraphrases a card it already filed ("data engineer at Initech" vs "works at
  Initech as a data engineer"), or that re-files one since superseded, still writes a
  new row. Nothing is lost, but near-duplicates can accumulate until a human or the
  planned embedding-cluster consolidation sweep collapses them.
- **Synthesis facts** (an emergent fact no single chat states, e.g. "actively
  job-hunting") fail grounding *by definition* and are the most-governed, least-built
  layer: they must cite their evidence cards and carry lower trust. Today this
  exists as design rather than code.

Prior art and the wider design space: [REFERENCES.md](REFERENCES.md). How the layers
and read-limits fit together: [MEMORY_DESIGN.md](MEMORY_DESIGN.md).
