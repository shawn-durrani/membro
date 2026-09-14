- Restoring a snapshot no longer brings erased things back (#101). A
  snapshot is a point in time, and the erasure journal lives inside the
  database, so copying an older snapshot into place used to resurrect
  every fact, message and attachment erased since, tombstones and all.
  The new `scripts/restore_snapshot.py` keeps a copy of the live database
  first, copies the chosen snapshot in, then replays every journalled
  erasure the older copy lacks through the same erasers the danger zone
  uses, and appends those tombstones under their original times. It
  refuses while the service is running and prints counts, never content.
