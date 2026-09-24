- The MCP server's word-for-word chat search now refuses a session that
  wasn't given the owner's token, even when that token lives only in
  `.env`. It used to check against its own empty copy and let everyone
  through, and a token passed at registration was checked against itself.
  Register the sessions that should search with `-e MEMORY_AUTH_TOKEN`,
  as the docs say (#117).
