- Restoring a snapshot no longer brings back voice clips you deleted or
  people you forgot (#129). The restore replayed the erasures made since
  the snapshot but skipped the voice ones, so a deleted clip came back as
  a row with no audio behind it, and a forgotten person came back
  unforgotten. It now replays those too: every clip delete, including
  each one from the "clips crossband no longer uses" button, and every
  forget, with the person's clips, kept-clip lists and approved facts
  handled as the forget handled them. It then lists any clip whose audio
  file is missing, by person and clip id, and keeps it for you to judge.
  The dry run now counts the erasures it would replay by kind.
