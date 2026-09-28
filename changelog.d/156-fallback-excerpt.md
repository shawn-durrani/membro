- When search can't use its index, each hit now shows the words around
  the match instead of the message's first 200 characters (#156). That
  fallback serves a query the index can't parse, and every search while
  the index is out of step before a restart repairs it. Its excerpts are
  now the same 64 words, centred on the match and marked the same way.
