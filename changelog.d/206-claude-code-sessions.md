- Membro can keep a short note of each Claude Code session you run on
  this computer (#206). Turn on `claude_code_feed`, and once a session
  has been quiet for half an hour the miner model writes what you set
  out to do, what got done and what's left open. The note is kept as a
  chat from the `claude-code` app and mined like any other. The model
  sees only what you typed and what Claude wrote, with anything shaped
  like a key masked first, and SDK sessions are skipped. A new speaker
  kind, `record:<app>`, marks a note like this as an account of what you
  did, so the miner reads it as yours.
