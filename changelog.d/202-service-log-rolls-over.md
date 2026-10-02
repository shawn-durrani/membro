- The service log no longer grows without limit (#202). Each start
  checks `data/service.log`, and past 10MB it moves to
  `data/service.log.1` and a fresh log begins, the same way Crossband and
  Spendglass do it. Both files stay readable only by you.
