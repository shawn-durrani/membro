- The profile now drops a fact you've pulled without waiting for some
  other rebuild (#189). Erasing a fact or a message, or forgetting a
  person, rebuilds the profile straight away when it was built from one
  of their facts. A hold rebuilds it five minutes after your last hold,
  so a run of holds costs one rebuild. Nothing is spent when the profile
  never used the facts, and two rebuilds never run side by side.
