- The leak scanner is now a byte-for-byte copy of crossband's canonical
  (#99), and the suite fails when the copy drifts from it. The copy
  brings crossband's fixes: placeholder allowlists anchored to the whole
  token, so a masked tailnet no longer excuses a machine name in front
  of it; an inline `secret-scan: allow` marker for a deliberate keep; and
  tree hits that name the file they came from.
