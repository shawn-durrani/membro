- Membro no longer writes your recovery secret into its log. It used to
  print the secret on every start, and under launchd that output goes to
  `data/service.log`, so each restart added a plain-text copy. Now a
  configured `MEMORY_AUTH_TOKEN` is never printed, and a token minted for
  one start is printed only on a first run, before you've set a password.
  Copies already in an old log stay there until you clear the log (#113).
