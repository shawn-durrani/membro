- The profile now aims for most of its word budget (#96). The prompt
  used to give the model a ceiling and a warning against padding, and
  the profile settled at about half of its 2,000 words while the ledger
  held far more than that. The prompt now asks for a range, 80% of the
  budget up to the budget (`memory_summary_fill`; `0` turns the floor
  off), spent on the specifics the entries carry: dates, names of
  projects and places, numbers, the current state of each thread. A
  draft under the floor gets one expansion pass from the same entries,
  only when they hold at least twice the floor in words; a draft still
  short after that is kept, and nothing is invented. Each stored version
  now records which passes shaped it, and the admin page shows the fill
  as a percentage.
