- You can now erase a fact the automatic judge has looked at (#158). The
  judge keeps a record of each look that points at the fact, and the
  database refused to delete a fact while that record stood, so the
  eraser failed with a server error. The eraser now removes that record,
  which holds only an id and a time, along with the fact.
