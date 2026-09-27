- A restart or a deploy no longer signs you out of the admin page
  (#131). Sessions used to live only in the service's memory. They're
  now kept in the database as a SHA-256 hash with their 24 hour expiry,
  never the cookie itself, so a copy of the database can't sign anyone
  in. "Log out" still ends that session. A password reset now ends
  every other session too, where before only a restart did, and so
  does removing a passkey, so a lost phone's session goes with its
  passkey. `scripts/restore_snapshot.py` ends every session as well.
  The first start on this version signs everyone out once.
