# Memory integrity: why the walls exist

Most home-made AI memory is a markdown file that only grows. The trouble
isn't storage. A wrong line, once written, stays there for good and
quietly spreads. Membro's defences exist to stop that. A made-up example
shows how memory gets poisoned, and the four walls show how Membro stops
it. The example's names and facts are invented, but the failure is real,
and it's what the walls were built against.

## How a fiction spreads

Meet Sam. Sam's memory gets poisoned in three steps, and Sam never
meant any of it.

1. A roleplay plants a fiction. Sam starts a practice interview:
   "Pretend to be my interviewer. I'm a candidate based in Metropolis,
   so walk me through the questions." Metropolis is now in a chat, as a
   stage direction and not a fact about Sam. Sam lives in Riverdale, and
   has said so plainly in nine other chats.
2. A model turns the fiction into a claim. Later Sam asks a model "what
   do you know about me?" The model sums up loosely and writes "Sam is
   based in Metropolis." The miner reads that summary and files a card
   that says "Sam lives in Metropolis."
3. The lie multiplies. The miner is shown the current ledger, because
   that's how it avoids repeats and spots updates. Now Metropolis looks
   settled, so it gets restated and reinforced. It ends up on chats
   about a renovation, chats about scheduling, and chats that never
   named a city at all.

No message from Sam ever said Sam lives in Metropolis. One stage
direction in a roleplay became, through the model's own manners, a
"fact" that spread across the whole ledger. A memory kept in a markdown
file has no defence against this, and it's the bug the walls exist to
stop.

## Constrain the write path

The bug isn't that the model is malicious. A soft instruction, like
"here's the ledger, don't repeat entries", gets treated as a source of
facts, because models don't keep their context in neat compartments.
Asking the model more nicely can't fix that. Fixed checks on the write
path can, because they don't care how polite the model was. That's why
four walls run on every mined fact.

## The walls

A mined fact that trips grounding, temporal grounding or source trust is
written but quarantined. That means it's held out of recall and the
profile, and marked low confidence, and never quietly trusted. The owner
clears the review queue. The optional judge pass, off by default, can
also release a fact held only by grounding. It does that only when it
quotes the missing name from the chat word for word.

### Grounding

The names in a fact must appear in the part of the chat that was mined
with it, which is up to 120 messages. "Sam lives in Metropolis", mined
from a chat where Metropolis never appears, is quarantined. This catches
the spread, step 3. The owner's name, the everyday names listed in
`grounding_allowlist`, and every name or alias on the People page always
pass.

### Temporal grounding

This is the same rule for time. A fact may not state a calendar date or
a schedule relative to now that its source never stated. Grounding
checks names only, so dates need a wall of their own. Say a fact reads
"the appointment is Saturday (tomorrow from the conversation date)", and
it was mined from "on Saturday, roughly nine days away". It keeps the
real weekday and invents the interval, and grounding alone would pass it
at high confidence. The biography is right in broad strokes, and the
plans for the next few days are wrong.

Dates compare by meaning, so "June 30" in the source grounds
"2026-06-30" in the fact. A fact that keeps the source's own phrase word
for word always passes, and that's what the wall steers toward. The
reader then works the phrase out against the fact's date, and doesn't
trust a guess the service made when it saved the fact.

### Source trust

Biography isn't mined from roleplay, a persona or interview practice, or
from a model's own "what do you know about me" summary. This catches the
seed, steps 1 and 2, even when the name does appear in the source. The
wall checks a list of phrases, such as "pretend to be", "role-play",
"mock interview" and "what do you know about me". It reads only what the
people in the chat said, so a model's own advice, like "rehearse your
set", doesn't make a chat roleplay. Wording outside the list relies on
the miner's instructions, which tell it to skip roleplay too.

### System meta

This wall drops a line, where the others hold one. A "fact" about
Membro's own machinery or the AI tools, like "the memory ledger
quarantined 100 facts", "the grounding wall over-flagged" or "PID 4321",
isn't biography about you, so there's nothing to review. Holding it
would only flood the queue when a conversation happens to be about the
memory system, which has happened. Such lines are never extracted.

The wall is a short list of keywords, and a match drops the line
outright. The list includes "Claude Code", and "quarantined" and
"superseded" in any sense, so "Sam uses Claude Code every day for work"
is dropped too. A second keyword list drops engineering chatter: pull
requests and issues, code review and CI status, hotfixes, rebases, stack
traces and styling tweaks. A plain verb like "deployed" or "committed"
isn't on either list. Anything that misses both lists stays eligible,
and goes through the three quarantine walls, so at worst it lands in
review. The message itself always stays in the tapes, so nothing is
lost.

## Why the miner still sees the ledger

It's tempting to fix contamination by hiding the ledger from the miner.
That breaks spotting repeats and updates. Without the ledger, the miner
can't tell "already known" from "new", or see that one fact updates
another. The
ledger stays visible, and the walls make the write safe.

## What's guaranteed

These hold in the code, in `walls.py` and `mining.py`, and the tests
hold them: `test_walls.py`, `test_mining.py`,
`test_meta_conversation.py`, `test_invariants.py`,
`test_guest_speakers.py`, `test_miner_tags.py`, `test_message_erase.py`
and `test_restore.py`.

- Facts are only added to. No automatic path deletes a fact, which can
  only be superseded, quarantined or dismissed. A fact is hard-deleted
  only when a person presses the eraser, or when a restore replays that
  same erasure.
- The tapes are the ground truth, and no pass ever changes them. That's
  what makes a bad card possible to spot, and to trace to its source.
  The one exception is the same human hand. The owner can erase a
  single message, with `DELETE /v1/messages/{id}`, or a single file.
  Live facts bound to an erased message move to review, and don't
  vanish. The message's files stay until they're erased themselves.
  Every erasure, of a fact, a file, a message or a voice clip, leaves a
  row in the `erasures` journal that holds no content. What was erased
  is gone, and the record that it was erased stays.
- External writes, from any MCP client, are quarantined when they're
  created, whatever they claim to be. That's the same rule, constrain
  the write, applied to who wrote it.
- A mined fact's event date is the date the fact is about. It's kept
  only when the miner names the single message it drew the fact from,
  and that message's text holds an explicit calendar date for it. A date
  the model made up, or one that only sits elsewhere in the same chat,
  is refused outright, and the fact is dated to the conversation. If
  the phrase names no year, like "June 30", the year comes from the
  conversation, never from the model, so no part of the date is a
  guess. A made-up date corrupts a timeline as quietly as a made-up
  city corrupts a biography.
- The same rule covers a card's wording, as well as its date field. A
  card that states a calendar date or a relative schedule its source
  never stated is quarantined for review. The schedules it catches are
  words like "tomorrow" and "next Saturday", and intervals written in
  digits, like "in 9 days". An interval in words, like "in nine days",
  isn't caught.
- A card's source turn is recorded only when it's real. A mined card
  points at one message only when the miner named that message. In a
  window with a guest or an unidentified speaker, a card with no source
  gets one re-ask. The turn the re-ask names must share a distinctive
  word with the card, and match it better than any turn from a guest or
  an unidentified speaker. A tie is refused. A card that can't be tied
  to a turn is stored unbound, and when it's held, the review queue
  shows it has no source turn. In a chat with only the owner and the
  models, an unbound card with nothing else wrong goes straight into the
  ledger. Wrong provenance is quieter than a wrong fact, and harder to
  undo. It makes a guest's sentence, or a summing up of several turns,
  read like something the owner said.
- A re-mine can't duplicate a card that's still current. Two mining
  passes over one conversation can't overlap, because each conversation
  has its own lock. A pass that writes facts, then dies before recording
  how far it read, adds nothing when it runs again. A card with the same
  words from the same conversation collapses onto the row already there,
  whether that row is trusted or held. The match ignores case and
  punctuation, and that's what the crash and retry case needs.
  Duplicates are more than clutter, because five copies of one wrong
  card read like five confirmations. A reworded re-mine is a different
  problem, and it's listed under [Limitations](#limitations).

### Supersession only moves forward in time

The miner may propose that a new fact retires a listed one, and two
guards limit what a proposal can do. A fact whose grounded event date is
more than a day older than its target's can't supersede it. Mining
imported or re-mined history then files old claims as dated history, and
never as replacements for newer truth. A fact held for review can't
change the trusted facts at all. Its replacement waits, and shows up
only as a count in the distill result, for a person to apply or ignore.
Both guards bind the automatic path only. The owner's Supersede in
review has no such limits.

### Limitations

The public issues track each of these.

- The walls are built to catch nearly everything, and they can be
  wrong. They flag for review, and never reject outright. In the
  original tuning they flagged about 12% of facts that were valid, and
  nobody has measured that since. A stricter check by meaning is
  planned.
- Recall ranks current facts over stale ones, and doesn't resolve
  contradictions. Retiring a stale fact takes a human supersede, or a
  future consolidation pass.
- A deferred supersession, one proposed by a held fact, is reported
  only as a count in the distill result. It doesn't travel with its
  fact into the review queue, so the reviewer applies or ignores it by
  hand.
- The re-mine guard matches the same words within one conversation. A
  pass that rewords a card it already filed, like "data engineer at
  Initech" and "works at Initech as a data engineer", still writes a new
  row. A pass that re-files a card since superseded writes one too.
  Nothing is lost, but near duplicates can pile up until a person, or
  the planned sweep that clusters cards by meaning, collapses them.
- Synthesis facts are facts no single chat states, like "actively job
  hunting". They fail grounding by definition, and they're the most
  governed and least built layer. They must cite their evidence cards,
  and carry lower trust. That exists as design only, with no code yet.

Prior art and the wider field are in [REFERENCES.md](REFERENCES.md).
How the layers and the limits fit together is in
[MEMORY_DESIGN.md](MEMORY_DESIGN.md).
