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
`--no-hint` drops the sentence, to check how much it moves the score.

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

**Pilot, 12 questions, not a LongMemEval score.** Two questions of each
type, drawn with `--seed 1`, run on 27 September 2026 against membro
`ea97e81` and crossband `ff3fb05`, with crossband's default seat.

| Question type | Correct |
|---|---:|
| knowledge-update | 2 of 2 |
| multi-session | 0 of 2 |
| single-session-assistant | 1 of 2 |
| single-session-preference | 0 of 2 |
| single-session-user | 1 of 2 |
| temporal-reasoning | 2 of 2 |
| All twelve | 6 of 12, 50% |

Two questions per type can't tell a strong type from a weak one. The pilot
shows the whole pipeline works on real questions and measures what a
question costs. A score needs all 500.

Every question was answered and graded, and none failed. Membro mined
between 15 and 59 facts from each history, 35 on average, and held about
one per history for review. Both preference questions got good general
advice that left out what the history said about the person, which is the
failure the profile exists to prevent.

## What it costs

Every call in the pilot went through the meter, 1,957 of them, and each
was priced at list price from the token counts in its reply.

| What | Model | Per question |
|---|---|---:|
| Membro mining the history | `claude-haiku-4-5` | $0.221 |
| Membro rebuilding its profile | `claude-sonnet-5` | $0.043 |
| Membro's embeddings | `text-embedding-3-small` | $0.001 |
| Crossband's seat answering | `claude-opus-4-8` | $0.076 |
| Crossband's own small calls | `claude-haiku-4-5` | $0.002 |
| Grading | `gpt-4o-2024-08-06` | $0.001 |
| Total | | $0.343 |

At that rate all 500 questions cost about $172. A question takes about
167 seconds, 162 of them spent filling membro, so a full run takes about
23 hours one question at a time, or about 6 hours with `--jobs 4`. Mining reads about
200,000 tokens a question, which is the history plus the ledger so far,
once per conversation.

Reusing kept stores with `--stores` drops the cost of a rerun to the seat
and the grading, about 8 cents a question, or $39 for all 500.

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
saved chats. Read the score by type, and never as one number alone.

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

Mining isn't deterministic. One history gave 31 facts on one build and 38
on the next, so what the seat can find, and whether it answers, can change
between runs. A full run averages that out over 500 questions. One
question's result doesn't.

Costs are list prices applied to the token counts in each reply. They
aren't billed amounts. Prices are in `live/meter.py`, dated.

A store kept with `--stores` holds dates shifted to the day it was built.
Weeks later its facts have decayed a little, so rebuild old stores before
comparing runs.
