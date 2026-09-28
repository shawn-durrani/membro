- Erasing a photo now takes its caption's words out of the search index
  too (#154). Membro indexes a photo's auto-generated caption so you can
  search for what it showed, but the file eraser only removed the photo's
  empty text, so the caption's words stayed in the index and in later
  backups. Search never showed the erased photo, and restoring a snapshot
  had the same gap. Nothing on this install needs repair: no file had been
  erased yet.
