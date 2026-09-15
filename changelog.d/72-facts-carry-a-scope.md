- Every fact now carries a scope, and a guest's fact stays in its own
  conversation (#72, contract 1.6). A fact drawn from a guest's turn, or
  saved while guests were in the room, is recalled only from the
  conversation it came from and never joins the profile. Your own facts,
  and everything stored before this, are global, so recall behaves as it
  did. A client names its conversation on `/recall` to get the facts
  bound to it. The review queue says which chat a bound fact belongs to,
  approving keeps the binding, and "Recall everywhere" is the one way to
  widen it.
