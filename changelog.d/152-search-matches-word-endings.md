- History search now matches word endings (#152). A search for "sister"
  finds "sisters", and "packed" finds "packing" and "packs". Both search
  indexes, for messages and for file text, are rebuilt with the new rule
  the first time the service starts. On a store the size of a busy one
  (about 43,000 messages) that takes about a second. Image captions stay
  searchable through the rebuild, which a plain index repair used to lose.
