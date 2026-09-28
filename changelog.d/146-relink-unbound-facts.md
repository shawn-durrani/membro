- A one-off repair gives mined facts back the message they came from
  (#146). From 13 August the miner stored most facts without it, because
  it couldn't read the model's bracketed tags (#145). Erasing that message
  then left the fact in recall instead of sending it back to review, and
  any date the model gave it was lost. `scripts/relink_unbound_facts.py`
  binds such a fact to one of your own turns. It does that only in a chat
  with no guests, and only when that turn clearly shares the most
  distinctive words with the fact. It moves the date only when that turn
  writes the fact's calendar date. It's a dry run unless you pass
  `--apply`, which refuses while membro is busy, keeps a copy of the
  database first, and notes each change without any text in
  `data/repairs.jsonl`.
