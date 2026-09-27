- The LongMemEval benchmark harness is in the repo, under `bench_memory/`.
  It gives each question a fresh copy of membro and crossband, started
  from git worktrees with their own data folders and ports. It loads that
  question's chat history into membro, asks crossband in a new chat, and
  grades the answer with LongMemEval's own prompts. Your real apps are
  never touched. A mock run is free and sends nothing off your computer.
  A paid run meters every model call and stops before the budget you give
  it. At list prices a question costs about 35 US cents, most of it membro
  mining the history. A 60-question sample, ten of each type, got 33
  right, and its table is in `bench_memory/METHODOLOGY.md` (#9).
