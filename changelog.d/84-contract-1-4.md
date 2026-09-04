- Memory contract 1.4 (#84): four additive changes so a client app can
  close seams the 2026-08-28 fleet audit found. `/search` hits carry
  `web_sources`, the domains the authoring turn read from, so archived
  web text can be marked untrusted when read back. `/health` reports
  `browser_origin`, the address a phone can open this service at, so a
  client links the eraser somewhere that works. A new
  `/conversations/{app}/{id}/watermark` route reports the highest
  message id held (erased ones included), so a client can wind its
  ingest watermark back after a restore instead of leaving a silent
  hole. `event_date` is now a calendar day at the owner's local
  midnight for every writer, with same-day recall ties broken on the
  save time. A 1.3 client keeps working unchanged.
