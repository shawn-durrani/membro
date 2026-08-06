#!/usr/bin/env bash
# Install (or refresh) the launchd user agent that keeps Membro running on
# macOS: it starts the service at login, restarts it within ~1s if it ever
# exits, and brings it back after a reboot (#111).
#
# Why Membro especially: when this service is down, chat clients keep answering —
# it just has no memory. No summary, no recall, and only a small state flag to
# say so. A silent capability loss is worse than a visible outage, so "someone
# will notice" is not a recovery plan. This was hit for real during a deploy:
# the service was killed to pick up new code and simply stayed dead.
#
# Deliberately a boring, standard launchd-supervisor shape rather
# than a new design — one pattern to learn, one to maintain.
#
# Idempotent: safe to re-run after a `git pull`. It takes over cleanly —
# unloading any prior agent and stopping a hand-started instance first — so
# there is only ever ONE owner of the service process (which also ends the
# port-lock race, since two competing starts can no longer happen).
#
# Usage:  ops/install-supervisor.sh
# Undo:   launchctl bootout gui/$(id -u)/dev.membro.server
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd -P)"
LABEL="dev.membro.server"
TEMPLATE="ops/${LABEL}.plist.template"
DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PORT="${MEMORY_PORT:-8901}"
DOMAIN="gui/$(id -u)"

[ -f "$TEMPLATE" ] || { echo "✗ template not found: $TEMPLATE" >&2; exit 1; }

# A clean PATH for the agent: launchd gives an agent almost nothing, and
# start.sh needs python3 (Homebrew or system) to build/refresh the venv.
AGENT_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"
if PY="$(command -v python3 2>/dev/null)"; then
  AGENT_PATH="$(dirname "$PY"):$AGENT_PATH"
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s#{{REPO_DIR}}#${REPO_DIR}#g" \
    -e "s#{{HOME}}#${HOME}#g" \
    -e "s#{{PATH}}#${AGENT_PATH}#g" \
    "$TEMPLATE" > "$DEST"

# The generated plist must be valid and fully substituted before we load it.
plutil -lint "$DEST" >/dev/null
if grep -q '{{' "$DEST"; then
  echo "✗ unsubstituted placeholder left in $DEST" >&2; exit 1
fi

# Take over as sole owner: drop any prior agent, then stop a hand-started
# instance still holding the port, before bootstrapping the agent (RunAtLoad
# starts the one real instance).
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
sleep 1
if PID="$(lsof -tnP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)"; then
  echo "· stopping hand-started instance (pid $PID) so the agent owns the port"
  kill "$PID" 2>/dev/null || true
  for _ in $(seq 1 15); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$PID" 2>/dev/null || true
fi
launchctl bootstrap "$DOMAIN" "$DEST"
launchctl enable "$DOMAIN/$LABEL"

echo "✓ supervisor installed — Membro will self-restart and survive reboots."
echo "  status:  launchctl print $DOMAIN/$LABEL | grep -iE 'state|pid|program'"
echo "  logs:    tail -f $REPO_DIR/data/service.log"
echo "  remove:  launchctl bootout $DOMAIN/$LABEL"
