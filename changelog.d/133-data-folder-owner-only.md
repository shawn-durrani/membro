- Backups are private to your own account on the Mac. The database was
  already, but each backup, the backups folder and the service log could
  be read by any other account once it got past the data folder, and a
  backup copied elsewhere kept that open mode. At startup the service now
  takes that access away from everything in the data folder, and every
  file it makes after that is private from the start. The restore and
  the maintenance scripts do the same (#133).
