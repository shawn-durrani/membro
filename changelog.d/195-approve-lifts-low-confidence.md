- Approving a held fact now lifts the low confidence its hold gave it
  (#195). A fact held by the write gate or by the miner's checks is
  marked low so nobody mistakes it for a checked one, and approving it
  used to leave the mark, so every AI still read it as doubtful. It now
  comes back high, like a fact nothing held. Other holds keep the
  confidence the fact was saved with. For facts approved before this
  change, `scripts/raise_approved_confidence.py` does the same, but only
  for those the service log shows you approved and never edited.
