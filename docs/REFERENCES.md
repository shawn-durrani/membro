# References

The memory design implements mechanisms from published research. Credit where it's
due, and honesty about what we verified: findings below are marked **implemented**,
**planned**, or **refuted** (claims our own adversarial verification pass could not
support; we use the mechanism but not the claimed numbers).

## Implemented

- **Temporal validity / bi-temporal facts**: every fact carries the date it is
  *about* (`event_date`) separately from when it was written; superseded facts are
  kept as history, never deleted.
  *Zep / Graphiti: temporal knowledge graphs* ([arXiv 2501.13956](https://arxiv.org/abs/2501.13956))
- **Bounded recency in recall**: newer facts rank above stale-but-similar ones
  without hiding history (it ranks; it does not resolve contradictions).
  *MemoryBank: Ebbinghaus-style decay `R = e^(−t/S)`*
  ([arXiv 2305.10250](https://arxiv.org/pdf/2305.10250) ·
  [AAAI 29946](https://ojs.aaai.org/index.php/AAAI/article/view/29946))
- **Summary as cache, ledger as truth**: the injected profile is rebuilt from the
  ledger and never treated as the source of record; the full history stays
  retrievable. *MemGPT: working context vs. retrievable archive; its DMR results
  motivate keeping full history reachable*
  ([arXiv 2310.08560](https://arxiv.org/pdf/2310.08560))
- **Recency × importance summary selection** (`weighting.py`): facts are scored
  `importance × exp(−age/half_life)` on the fact's true **event_date**, so
  bulk-imported old facts sort as old whatever their insertion order.
  *Generative Agents (recency · importance; poignancy scoring; we drop the
  relevance term, since a query-less summary fold has no query)*
  ([arXiv 2304.03442](https://ar5iv.labs.arxiv.org/html/2304.03442)); decay form
  from *MemoryBank* `R = e^(−t/S)`.
- **LLM-assigned 1–9 importance at extraction** (10 is owner-only; see the
  next bullet), with anchored examples to tame score noise (the verified
  caveat). *Generative Agents.*
- **Importance-adaptive decay**: half-life stretches ~4× from importance 1 to
  9 (22.5d → 82.5d), so life-defining facts outlive mundane ones without
  hiding history. Above the stretch sits a **permanence tier of our own**:
  importance 10 is owner-only and pinned, with an infinite half-life and an
  unconditional place in the profile's durable pool, exempt from its 200-slot
  competition. Over a years-long horizon every finite multiplier reaches zero,
  so permanence has to be a separate tier rather than a stretched half-life;
  the miner is capped at 9 so nothing automated can mint it.
  *FadeMem (mechanism verified; its benchmark numbers refuted, see below)*
  ([arXiv 2601.18642](https://arxiv.org/pdf/2601.18642))
- **Durable/active pool separation**: a fixed age-blind pool (by importance) and
  a scored active pool feed the summary as distinct groups, so stable history and
  live threads can't starve each other. A light form of *MemGPT's* working-context
  separation; full per-section budgets remain planned.
- **Paraphrase collapse before summary selection**: near-duplicate facts (cosine)
  collapse to their best copy at selection time, so frequency can't masquerade as
  salience; the ledger keeps every copy. Same cutoff recall uses at read time.
- **Emergent topic sections in the profile**: the profile keeps a fixed
  stable→volatile spine (the MemGPT-style separation above:
  Identity/Preferences/Relationships … Goals/Recent Changes),
  but its middle sections are named by the model from what the facts actually
  cluster around, appearing and dissolving as life changes. *A-MEM:
  Zettelkasten-style organisation created by the model rather than a
  predefined schema* ([arXiv 2502.12110](https://arxiv.org/abs/2502.12110));
  the derive-abstractions-from-clusters idea also appears in *Generative
  Agents*' reflections. Honest caveat: no published work A/Bs fixed vs.
  emergent *profile headings* specifically; adjacent evidence only. It shipped
  behind a config flag as a reversible experiment (2026-07-09) and graduated
  to the only layout on 2026-07-26 on the strength of its live output rather
  than a measurement; no A/B was ever run. Every rebuild is still a kept,
  restorable version, so the judgement remains revisitable.

## Planned

- **Per-section budgets** (the full MemGPT-style structural separation) and
  **dynamically reallocated** budgets when one section runs hot.
- **Saturating frequency boost** `f/(1+f)` over topic clusters (needs
  consolidation v2's embedding clustering). *FadeMem.*
- **Reinforce-on-reuse** (recalled facts strengthen). *MemoryBank "spacing effect."*
- **Half-life tuning on real data**: the 30-day base × importance stretch is a
  researched starting point rather than an empirically tuned constant.

## Considered / counter-examples

- *A-MEM* (relevance-only retrieval + memory evolution): a useful counter-example
  to pure-recency designs; its schema-free organisation idea graduated to
  "Implemented" above as the profile's emergent topic sections
  ([arXiv 2502.12110](https://arxiv.org/abs/2502.12110))
- *WMR formalisation* (Frontiers in Psychology)
  ([PMC12092450](https://pmc.ncbi.nlm.nih.gov/articles/PMC12092450/))

## Refuted in our verification (mechanisms kept, numbers rejected)

- "Selective forgetting improves long-term recall (FadeMem beats Mem0/MemGPT on
  LoCoMo, 82% retention)": failed 0–3 in adversarial review; single vendor
  simulation. We use FadeMem's *decay/frequency mechanisms*, not its benchmarks.
- "MemoryBank ties decay to access/importance, dropping low-importance old items":
  overstated versus the primary text (1–2).
- "Full retrievable history beats a lossy summary (32%→92%)": only partially
  survived (1–2); treated as directional support for ledger-as-truth rather than
  as a quantitative claim.

## Design lineage

The append-only + extraction-walls + quarantine design grew out of a real
contamination incident in a predecessor system (a roleplay persona becoming
"canon" and spreading across unrelated conversations); the synthetic write-up of
that failure mode and the walls that stop it is
[MEMORY_INTEGRITY.md](MEMORY_INTEGRITY.md).
