- Memory contract 1.8 (#127): the People page shows, for each person, how
  many stored voice clips crossband no longer uses, with a button to
  delete them, and a total with one button for everyone. Membro kept every
  clip crossband ever sent, so one person's voice could fill hundreds of
  clips where crossband uses 15. Crossband now sends the list of clips it
  keeps each time it syncs, and the count is the stored clips missing from
  it. The list alone deletes nothing: a clip goes only when you press, and
  each one leaves a content-free line in the erasure journal. A clip
  crossband drops is deleted as an ordinary clip delete, which now says
  why it went (rotation, settled or set aside) in the journal. A 1.7
  client keeps working, and without a list nothing is counted.
