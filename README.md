# Membro

Membro is a local-first memory service for AI assistants. It ingests
your conversations (including a claude.ai export), mines them for
durable facts behind deterministic extraction walls, and serves the
result to local models through a small HTTP API and an MCP server:
ranked recall, verbatim history search, and a rebuilt profile summary
with per-fact provenance. Everything lives in one SQLite file on your
machine. Nothing listens beyond loopback unless you deliberately widen
it to your own tailnet.

## Guarantees

- Facts are superseded, quarantined, or dismissed, never deleted by
  automation. The only hard delete is a button in the UI.
- Every mined fact passes grounding, temporal, and source-trust checks
  before entering canon. Doubtful facts are held for your review.
- Ingested messages are immutable. The summary is a cache rebuilt from
  the ledger, and each claim traces to its source facts.
- With no API keys, ingest, search, and keyword recall still work; LLM
  paths raise clear errors instead of degrading silently.

## Requirements

- Python 3.12+
- Optional: an Anthropic key (mining, summaries) and an OpenAI key
  (embeddings for semantic recall).

## Quick start

```sh
git clone https://github.com/shawn-durrani/membro.git
cd membro
./start.sh
```

The server runs at **http://127.0.0.1:8901**; set `MEMORY_PORT` if that
port is taken. First run prints a recovery secret; use it once to enrol a
password, then log in with the password. Set `MEMORY_AUTH_TOKEN` in
`.env` to keep the secret stable.

To import your claude.ai export:

```sh
.venv/bin/python scripts/import_claude_export.py /path/to/export.zip
```

Imports are idempotent; re-running never duplicates messages.

## MCP

```sh
claude mcp add -s user membro -e PYTHONPATH=<repo> -- <repo>/.venv/bin/python -m memory_service.mcp_server
```

Four tools: `recall_memory`, `search_history`, `save_memory`,
`memory_summary`. MCP saves carry an `mcp:*` origin and are always
quarantined for review; agents cannot write into canon.
`memory_service.mcp_admin_server` is a separate opt-in read-only pair
authenticated with the bearer token.

## Review queue

Wall-flagged and untrusted facts wait in the web UI: approve, dismiss,
quarantine, or supersede, all reversible. The miner scores importance
1 to 9; importance 10 never decays and only you can assign it. The
Mathematics page shows the ledger as geometry and never shows fact
content.

## Operations

Snapshots land in `data/backups/` on a timer with rotation and an
optional mirror folder (`MEMORY_MIRROR_DIR`).
`ops/install-supervisor.sh` installs a launchd agent; restart with
`launchctl kickstart -k gui/$(id -u)/dev.membro.server`. Everything the
service knows lives under `data/`; back that folder up and treat it as
sensitive.

Widening beyond loopback is tailnet-only and opt-in: read
[SECURITY.md](SECURITY.md) first. Never expose the port to the open
internet.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md): the settled decisions.
- [docs/MEMORY_DESIGN.md](docs/MEMORY_DESIGN.md): the memory model.
- [docs/MEMORY_INTEGRITY.md](docs/MEMORY_INTEGRITY.md): the extraction
  walls.
- [docs/API.md](docs/API.md): the HTTP contract.
- [docs/TUNING.md](docs/TUNING.md): every knob and its default.
- [docs/TESTING.md](docs/TESTING.md): what the suite guarantees.
- [docs/REFERENCES.md](docs/REFERENCES.md): research lineage, including
  claims we checked and rejected.

## Licence

[MIT](LICENSE).
