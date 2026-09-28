- The miner now reads its tags when the model copies the template's angle
  brackets, like `src=<3> importance=<7>` (#145). It couldn't read that
  form, so it asked the model again for every such fact's importance, and
  it stored the fact without the message it came from, which also dropped
  any date the model gave. In one benchmark run that was 459 of 770 fact
  lines. A placeholder copied whole, like `event=<YYYY-MM-DD>`, still reads
  as no date.
