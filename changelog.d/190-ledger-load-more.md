- The Ledger's Load more now works however many facts you have (#190).
  It used to ask for a bigger page on each press, and the server refuses
  a page over 1,000, so the fifth press failed. Each press now asks for
  the next 200 facts older than the last one on screen. `GET /v1/facts`
  takes an optional `before` fact id for this.
