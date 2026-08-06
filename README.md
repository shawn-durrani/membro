# Membro

Membro is a local-first memory service for AI assistants. It ingests your
conversations (including your claude.ai export), mines them for durable
facts with strict extraction walls, and serves the result back to any
local model through a small HTTP contract and an MCP server: a ranked
recall feed, verbatim history search, and a continuously rebuilt profile
summary with per-fact provenance. Everything lives in one SQLite file on
your machine; nothing listens beyond loopback unless you deliberately
widen it to your own tailnet.

## What makes it trustworthy

- **Append-only ledger.** Facts are superseded, quarantined, or dismissed;
  never silently deleted. The only hard delete is a button a human
  presses. You can always audit backwards.
- **Extraction walls.** Every mined fact passes deterministic checks
  (grounding, temporal grounding, source trust) before it can enter
  canon; anything doubtful is written and held for your review, never
  silently trusted and never silently dropped.
- **The transcript is ground truth.** Ingested messages are immutable;
  the profile summary is a cache rebuilt from the ledger, with every
  claim traceable to its source facts.
- **Honest degradation.** With no model key, ingest, search, and recall
  all work (keyword-only); LLM paths fail loudly rather than pretending.

## Requirements

- Python 3.12+
- Optional: an Anthropic key (fact mining, summaries), an OpenAI key
  (embeddings for semantic recall). Both absent is fine.

## Quick start

```sh
git clone https://github.com/shawn-durrani/membro.git
cd membro
./start.sh
```

The server binds **http://127.0.0.1:8901**. On first run it prints a
recovery secret to the terminal; open the page, enrol a password with it,
and log in. The recovery secret stays out of band afterwards (password
reset and MCP bearer auth only). To keep it stable across restarts, set
`MEMORY_AUTH_TOKEN` in `.env`.

Bring your history with you: export your claude.ai data, then

```sh
.venv/bin/python scripts/import_claude_export.py /path/to/export.zip
```

Ingest is idempotent, so re-running an import never duplicates messages.

## Connect your agents (MCP)

```sh
claude mcp add -s user membro -e PYTHONPATH=<repo> -- <repo>/.venv/bin/python -m memory_service.mcp_server
```

Four tools: `recall_memory`, `search_history`, `save_memory`,
`memory_summary`. Every save carries an `mcp:*` origin and is quarantined
for your review by construction; an agent cannot write directly into
canon. A separate, opt-in read-only admin pair
(`memory_service.mcp_admin_server`) speaks HTTP with the bearer token.

## The review queue

Untrusted or wall-flagged facts land in a review queue in the web UI:
approve, dismiss, quarantine, or supersede, all reversible. Importance is
scored 1 to 9 by the miner; 10 is owner-only permanence that never
decays. The Mathematics page visualises the ledger as geometry (ids,
ages, importances, clusters) and never exposes fact content.

## Running it for real

The server schedules its own snapshots into `data/backups/` (rotation,
change detection, optional mirror folder via `MEMORY_MIRROR_DIR`).
`ops/install-supervisor.sh` installs a launchd agent so the service
survives reboots and crashes; restart it with
`launchctl kickstart -k gui/$(id -u)/dev.membro.server`. Everything the
service knows lives under `data/`; treat that folder as sensitive in its
entirety and back it up.

Widening beyond loopback is opt-in and tailnet-only: see
[SECURITY.md](SECURITY.md) before you do, and never expose the port to
the open internet.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md): the settled design decisions.
- [docs/MEMORY_DESIGN.md](docs/MEMORY_DESIGN.md): the memory model in
  plain English (what is capped, what never is, how selection works).
- [docs/MEMORY_INTEGRITY.md](docs/MEMORY_INTEGRITY.md): the extraction
  walls and why they exist.
- [docs/API.md](docs/API.md): the full HTTP contract.
- [docs/TUNING.md](docs/TUNING.md): every knob, with defaults explained.
- [docs/TESTING.md](docs/TESTING.md): what the test suite guarantees.
- [docs/REFERENCES.md](docs/REFERENCES.md): the research lineage,
  including claims we checked and rejected.

## Licence

[MIT](LICENSE).
