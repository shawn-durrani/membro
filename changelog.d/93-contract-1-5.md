- Memory contract 1.5 (#93): a model's direct save now says who was in
  the room. `POST /facts` may carry `guest_speakers`, the same
  `guest:<name>` and `guest:unknown` values `/ingest` uses, and a stamped
  save is held for review under a new group, "Saved while a guest was
  in the room". The mined path already held a guest's words because
  each message names its speaker; the save tool named nobody, so a
  guest's claim relayed by a model reached canon unheld. A save that
  also read the web keeps its web-derived group with the guest clause
  appended. A 1.4 client keeps working unchanged.
