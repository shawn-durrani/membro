- Mining no longer loses facts on a model that thinks before it answers
  (#167). Claude Sonnet 5.5 and the other newer models think unless a
  request turns it off, and the thinking shares the answer's room, so
  the miner's reply could run out partway and the rest of the chat went
  unmined. The miner still thinks on a model that does by default, as it
  was benchmarked, and now gets 4,000 tokens for it on top of the room
  for its facts. Its retries, captions, the sweep and the judge's
  witness check turn thinking off where the model allows it, and get
  more room where it can't be turned off. Every model call checks how
  its reply ended. A miner reply cut short or refused is mined again in
  smaller pieces, and a retry cut short holds its fact for review. The
  judge's roleplay check asks each model for the thinking it takes, so
  `judge_model` can name any model. Claude Haiku 4.5 gets the same
  requests as before.
