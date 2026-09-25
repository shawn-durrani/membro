- Backups no longer stall while the Mac sleeps. The six hours between
  automatic snapshots used to count only time the Mac was awake, so a
  laptop that slept most of the day could go days without a new restore
  point. The timer now checks every five minutes and goes by the clock,
  so a snapshot that fell due during sleep is taken within five minutes
  of the Mac waking. An unchanged database still gets no new copy (#123).
