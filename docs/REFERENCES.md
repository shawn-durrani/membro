# References

Membro's memory design uses mechanisms from published research. Each
finding is credited, and marked as implemented, planned or refuted.
Refuted means an adversarial check here couldn't support the claim, so
Membro uses the mechanism and doesn't rely on the published numbers.

## Implemented

- **Facts with two kinds of time.** Every fact carries the date it's
  about, `event_date`, apart from when it was written. Superseding a
  fact never deletes it, and it's kept as history. Only the owner's
  eraser deletes a fact. *Zep and Graphiti, temporal knowledge graphs*
  ([arXiv 2501.13956](https://arxiv.org/abs/2501.13956)).
- **A small boost for newer facts in recall.** Newer facts rank over
  stale ones that look similar, without hiding history. Recall ranks,
  and doesn't resolve contradictions. The boost is linear, at most 0.10,
  and gone once a fact is about three years old. The exponential decay
  credited here, `R = e^(−t/S)`, is the one profile selection uses.
  *MemoryBank, decay in the style of Ebbinghaus*
  ([arXiv 2305.10250](https://arxiv.org/pdf/2305.10250),
  [AAAI 29946](https://ojs.aaai.org/index.php/AAAI/article/view/29946)).
- **The profile as a cache, and the ledger as the truth.** The profile
  given to models is rebuilt from the ledger, and never treated as the
  record. The full history stays reachable. *MemGPT, working context
  and a retrievable archive.* Its deep memory retrieval results are why
  Membro keeps the full history reachable
  ([arXiv 2310.08560](https://arxiv.org/pdf/2310.08560)).
- **Recency times importance, to choose facts for the profile.** In
  `weighting.py` each fact scores `importance × exp(−age/S)`, counted
  from its true `event_date`. A bulk import of old facts sorts as old,
  whatever order the rows went in. *Generative Agents*, which scores
  recency, importance and relevance, with importance rated as
  poignancy. Membro drops the relevance term, because a profile built
  without a question has nothing to be relevant to
  ([arXiv 2304.03442](https://ar5iv.labs.arxiv.org/html/2304.03442)).
  The decay's form is *MemoryBank*'s `R = e^(−t/S)`.
- **Importance rated by a model, from 1 to 9, at extraction.** Anchored
  examples tame the noise in the scores, which is the caveat the check
  confirmed. Importance 10 is the owner's alone. *Generative Agents.*
- **Decay that slows with importance.** The decay's time constant `S`
  stretches about 3.7 times from importance 1 to 9, from 22.5 days to
  82.5 days. That's a half-life of about 16 days to 57 days. Life
  defining facts outlast mundane ones, and history stays visible. On
  top of the stretch, Membro adds a permanence tier. Importance 10 is
  set only by the owner and pinned. It never decays, and every pinned
  fact that's current, not held and not bound to one chat gets a place
  in the profile, outside the 200 places the durable pool competes for.
  Two pinned facts that say the same thing collapse to one. Over years
  every finite stretch reaches zero, so permanence has to be a tier of
  its own. The miner is capped at 9, so nothing automatic can make a
  fact permanent. *FadeMem*, whose mechanism checked out and whose
  benchmark numbers didn't, as set out in
  [the refuted claims](#refuted-in-a-check-here)
  ([arXiv 2601.18642](https://arxiv.org/pdf/2601.18642)).
- **Separate durable and active pools.** A fixed pool of the 200 most
  important facts, whatever their age and newest first within a level,
  and a scored active pool go to the profile as separate groups. Stable
  history and live threads can't crowd each other out. That's a light
  form of *MemGPT*'s separation of working context. Each profile
  section already has a fixed word ceiling, and budgets that shift
  between sections are still planned.
- **Paraphrase collapse before choosing facts for the profile.** Near
  duplicate facts, by cosine similarity, collapse to their best copy
  when the profile is chosen. How often something was said can't pass
  for how much it matters. The ledger keeps every copy. The cutoff is
  the same one recall uses when it reads.
- **Topic sections that emerge in the profile.** The profile keeps a
  fixed spine from stable to changing, the same split the pools borrow
  from MemGPT. That's Identity, Preferences and Relationships at the
  top, through to Goals and Recent Changes. The middle sections
  are named by the model from what the facts cluster around, and they
  appear and fade as life changes. *A-MEM*, organisation the model
  creates instead of a fixed schema, in the style of a Zettelkasten
  ([arXiv 2502.12110](https://arxiv.org/abs/2502.12110)). *Generative
  Agents* also draws abstractions from clusters, in its reflections.
  The caveat is that no published work compares fixed and emergent
  profile headings directly, and the evidence is only next door to the
  question. Membro made this the only layout on the strength of its
  live output, and nobody has run a comparison of the two. Every
  rebuild is still a kept version you can restore, so the call stays
  open.

## Planned

- **Budgets that shift between sections.** Each section has a fixed
  word ceiling. The full structural separation in the style of MemGPT
  would give each section its own budget, and move budget to a section
  that runs hot.
- **A frequency boost that levels off**, `f/(1+f)`, over clusters of
  topics. It needs the embedding clusters from the second version of
  consolidation. *FadeMem.*
- **Reinforce on reuse**, where facts you recall get stronger.
  *MemoryBank*'s spacing effect.
- **Tuning the decay on real data.** The base of 30 days, stretched by
  importance, is a starting point from research, and hasn't been tuned
  on real use.

## Considered, and counter-examples

- *A-MEM*, retrieval by relevance alone, plus memories that evolve. It's
  a useful counter-example to designs built only on recency. Its idea of
  organisation without a schema is implemented, as the profile's
  emergent topic sections
  ([arXiv 2502.12110](https://arxiv.org/abs/2502.12110)).
- *A formalisation of WMR*, in Frontiers in Psychology
  ([PMC12092450](https://pmc.ncbi.nlm.nih.gov/articles/PMC12092450/)).

## Refuted in a check here

Membro keeps these mechanisms, and rejects the numbers.

- "Selective forgetting improves long-term recall, and FadeMem beats
  Mem0 and MemGPT on LoCoMo, with 82% retention." It failed adversarial
  review 0 to 3, and it rests on one vendor's simulation. Membro uses
  FadeMem's decay and frequency mechanisms, and not its benchmarks.
- "MemoryBank ties decay to access and importance, and drops old items
  of low importance." That overstates the primary text, 1 to 2.
- "Full retrievable history beats a lossy summary, 32% to 92%." That
  only partly survived, 1 to 2. Membro treats it as support for the
  ledger as truth in direction, and not as a quantitative claim.

## Where the design came from

The design of an append-only ledger, extraction walls and quarantine
grew out of a real contamination incident in a system that came before
Membro. A roleplay persona became "canon" there, and spread across
unrelated conversations. [MEMORY_INTEGRITY.md](MEMORY_INTEGRITY.md) is
the made-up write-up of that failure, and the walls that stop it.
