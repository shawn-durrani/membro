- `GET /v1/busy` (#95): whether a restart right now would interrupt work
  in flight, for the fleet's deploy watcher, which asks before a restart
  and waits while the answer is true. Open on loopback like `/v1/health`.
  `reasons` carries fixed labels only: a running distill, summary,
  consolidate or embeddings-projection job, a backup mid-copy, a judge
  pass, or the re-embed refill after an embedding model change. A mark
  older than an hour stops counting, so a hung thread cannot hold every
  deploy. The route sits outside the versioned client contract, so
  `contract_version` stays at 1.5.
