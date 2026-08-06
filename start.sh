#!/usr/bin/env bash
# start.sh — one-command boot for multi-model-memory.
# Creates .venv if missing, installs deps only when requirements.txt changed,
# refuses to start a second instance, warns loudly about missing API keys,
# then runs the FastAPI service on http://127.0.0.1:8901.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${MEMORY_PORT:-8901}"

# ---- load .env if present (keys stay out of the shell profile) ----
if [ -f .env ]; then
  # .env holds live API keys and the owner token: owner-only, always. Tightened
  # on every start, not just at creation, so a file made before this existed (or
  # copied in by hand) is repaired rather than left world-readable forever.
  chmod 600 .env 2>/dev/null || true
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# ---- refuse a second instance: the port is the lock ----
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ERROR: something is already listening on port $PORT — refusing to start a second instance." >&2
  echo "       (If that's a stale process: lsof -nP -iTCP:$PORT -sTCP:LISTEN  then kill it.)" >&2
  exit 1
fi

# ---- venv + deps (install only when requirements.txt changed) ----
if [ ! -d .venv ]; then
  echo "Creating virtualenv at .venv ..."
  python3 -m venv .venv
fi
STAMP=.venv/.requirements.stamp
if [ ! -f "$STAMP" ] || ! cmp -s requirements.txt "$STAMP"; then
  echo "Installing dependencies (requirements.txt changed or first run) ..."
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
  cp requirements.txt "$STAMP"
fi

# ---- loud, specific warnings about what degrades without keys ----
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
  warn "############################################################################"
  warn "# WARNING: no ANTHROPIC_API_KEY and no OPENAI_API_KEY set.                 #"
  warn "#                                                                          #"
  warn "# DEGRADED: fact mining (POST /v1/distill) and summary regeneration       #"
  warn "#           (POST /v1/summary/regenerate) will FAIL — they need a          #"
  warn "#           utility-model key (Anthropic for the default claude-haiku      #"
  warn "#           miner; OpenAI if you configure an OpenAI miner_model).         #"
  warn "# DEGRADED: semantic recall is OFF — /v1/recall falls back to             #"
  warn "#           keyword-only matching (needs OPENAI_API_KEY for embeddings).   #"
  warn "#                                                                          #"
  warn "# Everything else works: ledger, review queue, search, ingest, admin UI.   #"
  warn "# Copy .env.example to .env and add keys to enable full capability.        #"
  warn "############################################################################"
else
  if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    warn "WARNING: ANTHROPIC_API_KEY not set. The default miner model (claude-haiku)"
    warn "         cannot run: fact mining (/v1/distill) and summary regeneration"
    warn "         (/v1/summary/regenerate) will fail unless you configure an"
    warn "         OpenAI miner_model in config.json."
  fi
  if [ -z "${OPENAI_API_KEY:-}" ]; then
    warn "WARNING: OPENAI_API_KEY not set. Embeddings are disabled, so /v1/recall"
    warn "         degrades to KEYWORD-ONLY matching (no semantic recall)."
  fi
fi

# ---- optional: tailnet-only remote access via Tailscale Serve (#51 slice 2) ----
# Opt-in only (MEMORY_TAILSCALE_SERVE=1) — never enabled by default, and never
# Funnel/public exposure. Failure here (Tailscale absent, signed out, or a CLI
# version mismatch) is reported loudly but never blocks the service itself,
# which always keeps working on loopback regardless.
if [ "${MEMORY_TAILSCALE_SERVE:-0}" = "1" ]; then
  bash scripts/tailscale-serve.sh || true
fi

echo "Starting multi-model-memory on http://127.0.0.1:$PORT (admin UI at /) ..."
exec .venv/bin/python -m memory_service.api
