# How the benchmark works

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) is a published
test of long-term memory in chat assistants. Each of its 500 questions
comes with its own chat history of about 48 past conversations and 490
turns, most of them unrelated to the question. The assistant has to find
the one or two turns that matter and answer from them. The small set,
which the harness runs, fits each history into about 120,000 tokens.

The harness measures one thing: how often crossband, with membro as its
memory, answers a LongMemEval question correctly. A seat never sees the
history directly. It sees only what membro gives it: the profile in every
prompt, the facts recalled for the question, and whatever the seat finds
when it searches the saved chats.

## What happens for each question

1. A fresh membro starts from a worktree of membro's main branch, with an
   empty data folder and a config that trusts crossband's app name.
2. The harness writes each past conversation into membro, moved forward in
   time so the question is asked today.
3. Membro mines each conversation into facts, then rebuilds its profile
   once.
4. A fresh crossband starts from a worktree of crossband's main branch,
   pointed at that membro.
5. The harness opens a new chat with one seat in it, turns web research
   off, and sends the question with one extra sentence about abstaining.
6. The seat's reply is saved as the answer, and both apps are stopped and
   deleted.
7. Once every question is answered, `gpt-4o-2024-08-06` grades each answer
   with LongMemEval's own grading prompts.

## Why each step is the way it is

Each of these choices changes what the score means, so a score quoted from
the harness should carry them with it.

**One pair of apps per question.** Each question ships its own history.
Sharing one membro would let a question be answered from another
question's history, and the benchmark would stop measuring memory.

**A new chat with one seat.** A chat's transcript belongs to the whole
chat, so a reused chat would replay other seats' old turns into the
prompt. A chat created with one seat has nothing in it but the question.

**Mining is part of the run.** Writing a conversation into membro stores
the record. Recall and the profile read the ledger of mined facts, which
stays empty until mining runs. A seat asked about an empty ledger says it
doesn't know, which is right on the 30 unanswerable questions and wrong on
the other 470.

**Dates move forward.** LongMemEval's histories are dated 2023. Membro
ranks facts by importance times a recency decay, and after three years the
decay has done its work.

| Importance | Half-life | What's left after 1,200 days |
|---:|---:|---:|
| 3 | 37.5 days | 1.3e-14 |
| 5 | 52.5 days | 1.2e-10 |
| 9 | 82.5 days | 4.8e-07 |

A three-year-old fact sits in the ledger and never wins a ranking. The
harness moves each whole history by one amount, so its question is asked
now and every gap between conversations stays the same. That keeps the
temporal questions answerable. Pinning importance or turning decay off
would change membro itself. The score therefore describes memory of recent
conversations. Nobody has measured how membro does on conversations that
are years old.

**The abstention sentence.** Every question is followed by: "If the
conversation history does not contain the answer, say plainly that you do
not have that information rather than guessing." LongMemEval's 30
unanswerable questions are marked correct only when the seat declines.
`--no-hint` drops the sentence, to check how much it moves the score. On
the 20 preference and multi-session questions from the sample, answered
again from the same stores, dropping it made the seat search its saved
chats less often, not more. Preference went from 1 of 10 to 0, and
multi-session stayed at 4.

**Crossband's app name.** Crossband writes to membro under the app name
`multi-model-chat`. The throwaway membro trusts that name, as a membro
paired with crossband does. Membro holds writes from an app it doesn't
trust for review, and held facts stay out of recall and the profile.
Everything else in membro's config is the shipped default.

**Keys held back.** The throwaway apps get the Anthropic and OpenAI keys
and nothing else. A seat with web search could answer without memory.

**The seat model.** The seat is crossband's default Claude seat, which is
`claude-opus-4-8` today. `--model` picks another. A smaller model has less
general knowledge to cover for what memory missed, but it also fails more
often at working with what memory found, which understates membro.

**The grader.** LongMemEval grades each answer with a language model and
a prompt per question type. The harness sends the same prompts, the same
settings and the same model the upstream scorer's `gpt-4o` option uses,
and counts an answer correct when the reply contains "yes", as the scorer
does.

## Results

**A 60-question sample, not a LongMemEval score.** Ten questions of each
type, drawn with `--seed 1`, run on 28 September 2026 against membro
`ea97e81` and crossband `0756998`, with crossband's default seat.

| Question type | Correct |
|---|---:|
| knowledge-update | 6 of 10 |
| multi-session | 2 of 10 |
| single-session-assistant | 8 of 10 |
| single-session-preference | 1 of 10 |
| single-session-user | 7 of 10 |
| temporal-reasoning | 9 of 10 |
| All sixty | 33 of 60, 55% |

A published LongMemEval score covers all 500 questions, weighted the way
the dataset is. This sample weights every type equally and holds none of
the 30 unanswerable questions, so its 55% can't be set beside a published
score. With ten questions a type, one more or one fewer right moves that
type by ten points.

Every question was answered and graded, and none failed. Membro mined
between 8 and 76 facts from each history, 38 on average, and held about
two per history for review. Nine of the ten preference answers missed.
Two gave general advice. Three said memory held nothing on the subject and
asked, when the history did hold it. Four used something else they knew
about the person, not the detail the grader's rubric wanted. Carrying
preferences into every answer is the job the profile exists to do, so
it's the clearest thing the sample found.

### Keeping what's said in passing

The miner now keeps what people mention in passing: what they own and
use, what they've done, their home and their family. Reading the sample's
preference and multi-session misses showed most had never become a fact.
So the 20 questions of those two types were rebuilt from scratch on 29
September, before and after the change, with fresh stores each time.

| Question type | Before | After |
|---|---:|---:|
| single-session-preference | 1 of 10 | 4 of 10 |
| multi-session | 4 of 10 | 6 of 10 |

The after run also had four other fixes: longer history search excerpts,
search that matches word endings, a roleplay check that reads only what
people said, and tags read with or without brackets. So the rise isn't
all the miner's.

- Three preference answers came right because the fact they needed was
  now mined: the editing software you use, a class you took, and new parts
  on your bike.
- Three multi-session answers came right because a history search now
  shows the whole number: a price, a count of shoes, what two gifts cost.
  The miner still leaves one-off numbers out, as it should.
- One multi-session answer went wrong. The ledger held a doctor's name and
  "an ENT specialist" as two facts, and the seat counted two doctors where
  the history had one.

The ledger nearly doubled, from 38.5 facts a history to 73.8. Most of the
new facts are lasting ones: things people own and use, what they've done,
their interests and their family. One-off detail, like a budget for one
purchase or plans for this weekend, makes up about as large a share as
before, so there's more of it in all. Fewer facts were held for review, 12
across the 20 histories against 25.

Four of the ten asides the misses turned on are still dropped: the phone
you own, a new power bank, a cat that sheds, and new kitchen fittings.
Each came up inside a request for advice about it. Three more were half
kept, like three sisters without the brother.

## What it costs

Every call in the sample went through the meter, 10,052 of them, and each
was priced at list price from the token counts in its reply. The sample
cost $20.81.

| What | Model | Per question |
|---|---|---:|
| Membro mining the history | `claude-haiku-4-5` | $0.224 |
| Membro rebuilding its profile | `claude-sonnet-5` | $0.043 |
| Membro's embeddings | `text-embedding-3-small` | $0.001 |
| Crossband's seat answering | `claude-opus-4-8` | $0.077 |
| Crossband's own small calls | `claude-haiku-4-5` | $0.002 |
| Grading | `gpt-4o-2024-08-06` | $0.001 |
| Total | | $0.347 |

A question cost between 28 and 44 cents. At the average, all 500
questions cost about $173. A question takes about 166 seconds, 161 of them
spent filling membro. The sample ran with `--jobs 4` in 43 minutes, so all
500 would take about 6 hours that way, or about 23 hours one question at a
time. Mining reads about 200,000 tokens a question, which is the history
plus the ledger so far, once per conversation.

Since the miner started keeping what's said in passing, building a store
costs about a sixth more. On the 20 questions rebuilt on 29 September it
came to 31 cents a question, against 27 before.

Reusing kept stores with `--stores` drops the cost of a rerun to the seat
and the grading, about 8 cents a question, or $39 for all 500.

Membro's calls read nothing from Anthropic's prompt cache in a benchmark
run. The part of a mining prompt that repeats from call to call is the
miner's instructions and the question's ledger. A question's ledger holds
about 100 facts at most, so that part stays under the 4,096 tokens Claude
Haiku 4.5 needs before it caches anything. A four-question run with
`--seed 1` cost 37 cents a question with the cache layout and 37 without.

## How LongMemEval fits membro

LongMemEval tests whether an assistant can recover any detail from a long
history. Membro keeps a short, curated ledger of durable facts about you,
with a profile built from it. The two overlap but they aren't the same
job, and the question types show where.

| Question type | Questions | Fit |
|---|---:|---|
| single-session-preference | 30 | Best. The profile carries preferences into every prompt. |
| single-session-user | 70 | Good. Facts about you are what the ledger stores. |
| knowledge-update | 78 | Good. A newer fact supersedes an older one. |
| multi-session | 133 | Mixed. Counting across conversations needs every match, and recall returns the top few. |
| temporal-reasoning | 133 | Mixed. Facts carry dates, and the decay favours the recent end of a range. |
| single-session-assistant | 56 | Outside the ledger. These ask what the assistant said, and membro mines only facts about you. |

The assistant questions can be answered only when the seat searches the
saved chats. In the sample it did, for 8 of 10. The preference row went
the other way: 1 of 10, where the fit says it should be strongest. Read
the score by type, and never as one number alone.

## What the score is for

The benchmark checks for regressions. It isn't a target. A change to
membro needs a failure someone met in real use, and a better score can
back that change up but never justify it on its own. Some changes would
raise the score and make membro worse to live with:

- Mining short-lived numbers, like how many films you watched this month.
  LongMemEval asks for them. A ledger full of them is clutter.
- Weakening the decay. Only the benchmark's old dates need it.
- Trusting every app's writes. Holding an unknown app's writes for review
  is correct.
- Always committing to one answer. When the seat finds two values and asks
  which is right, LongMemEval marks it wrong. In real use asking is often
  better.

Misses that are also real defects are worth fixing, for example a doctor
you mentioned four times that never became a fact.

## Limits

Mining isn't deterministic. One history gave 31, 33 and 38 facts on three
builds, so what the seat can find, and whether it answers, can change
between runs. A full run averages that out over 500 questions. One
question's result doesn't.

Costs are list prices applied to the token counts in each reply. They
aren't billed amounts. Prices are in `live/meter.py`, dated.

A store kept with `--stores` holds dates shifted to the day it was built.
Weeks later its facts have decayed a little, so rebuild old stores before
comparing runs.
