#!/usr/bin/env bash
# scripts/tailscale-serve.sh — tailnet-only remote access for Membro (#51 slice 2).
#
# Maps the LOCAL loopback service (default 127.0.0.1:8901) onto its OWN HTTPS
# PORT on your OWN Tailscale tailnet via `tailscale serve`. This is the ONLY
# sanctioned way
# to reach Membro off-machine (see SECURITY.md's trust-boundary section) —
# this script never runs `tailscale funnel` and never opens a public port.
# Loopback binding, the owner password, and MEMORY_AUTH_TOKEN are all
# completely unchanged: the tailnet is an extra PRIVATE network path to the
# exact same authenticated service, not a new credential or a wider one.
#
# Opt-in, not automatic: start.sh only calls this when MEMORY_TAILSCALE_SERVE=1
# is set (see .env.example / docs/TUNING.md). Safe to re-run any time —
# `tailscale serve` is idempotent, and running with no Tailscale installed, or
# with it installed but signed out, degrades to a clear skip message rather
# than an error (the same "degrade loudly, never silently" rule start.sh
# already follows for missing API keys).
#
# Usage:
#   scripts/tailscale-serve.sh           # establish (or re-affirm) the route
#   scripts/tailscale-serve.sh --status  # report the current route only; changes nothing
set -uo pipefail

PORT="${MEMORY_PORT:-8901}"
# A dedicated HTTPS PORT, not a path under the tailnet root — Membro's admin
# UI links and fetches ABSOLUTE paths (`/math`, `/v1/attachments/…`), so under
# a `/membro` prefix the browser would resolve them to `https://<host>/v1/…`
# and hit whatever else is served at that tailnet root (anything else served
# at that tailnet root is a DIFFERENT application). Its own
# port gives Membro its own origin, so every absolute path lands on Membro and
# nothing in the app has to become prefix-aware.
TS_PORT="${MEMORY_TAILSCALE_PORT:-8443}"
TARGET="http://127.0.0.1:${PORT}"

warn() { printf '\033[1;33m%s\033[0m\n' "$*" >&2; }
ok()   { printf '%s\n' "$*"; }

# ---- locate the tailscale CLI, explicitly, never by guessing ----
# PATH first, then MEMORY_TAILSCALE_BIN if the operator named one. NO implicit
# hardcoded fallback: an earlier draft guessed the macOS app-bundle path, and a
# test that cleared PATH to simulate "no Tailscale" then found the real CLI on
# the developer's own machine and ran it (see the 2026-07-29 entry in
# DECISIONS.md). An explicit env var keeps that impossible while still letting a
# real operator use a CLI that is installed but off PATH — which on macOS is the
# NORMAL case: Tailscale.app ships a working CLI inside the bundle (verified
# 1.98.8, `serve` fully supported; #87 corrected the earlier claim otherwise).
TS_BIN=""
if command -v tailscale >/dev/null 2>&1; then
  TS_BIN="tailscale"
elif [ -n "${MEMORY_TAILSCALE_BIN:-}" ] && [ -x "${MEMORY_TAILSCALE_BIN}" ]; then
  TS_BIN="${MEMORY_TAILSCALE_BIN}"
fi

if [ -z "$TS_BIN" ]; then
  warn "Tailscale remote access: SKIPPED — no 'tailscale' CLI on PATH."
  warn "  Tailscale may still be installed and working: on macOS the app bundle"
  warn "  ships a CLI that simply isn't on PATH. Point this script at it —"
  warn "    MEMORY_TAILSCALE_BIN=/Applications/Tailscale.app/Contents/MacOS/Tailscale"
  warn "  (or put that directory on PATH), then re-run:"
  warn "    scripts/tailscale-serve.sh"
  warn "  No Tailscale at all? Install from https://tailscale.com/download and"
  warn "  run 'tailscale up' first."
  warn "  Membro is unaffected: it keeps serving on http://127.0.0.1:${PORT}."
  exit 0
fi

# ---- confirm tailscaled is running and this device is on a tailnet ----
if ! "$TS_BIN" status >/dev/null 2>&1; then
  warn "Tailscale remote access: SKIPPED — tailscaled isn't running, or this"
  warn "  machine isn't logged into a tailnet yet. Run 'tailscale up', then"
  warn "  re-run: scripts/tailscale-serve.sh"
  warn "  Membro is unaffected: it keeps serving on http://127.0.0.1:${PORT}."
  exit 0
fi

status_only=0
if [ "${1:-}" = "--status" ]; then
  status_only=1
fi

if [ "$status_only" -eq 0 ]; then
  ok "Configuring tailnet-only access: :${TS_PORT} -> ${TARGET} ..."
  # One form, one meaning: serve the whole origin on our own HTTPS port. The
  # earlier draft needed two attempts because the PATH-mount flag layout
  # differs between CLI versions; serving a port needs no path flag at all,
  # so that ambiguity is gone with it. Never touches `funnel`.
  err_file="$(mktemp)"
  trap 'rm -f "$err_file"' EXIT
  if ! "$TS_BIN" serve --bg --https="$TS_PORT" "$TARGET" 2>"$err_file"; then
    warn "Tailscale remote access: 'tailscale serve' failed. Error:"
    sed 's/^/    /' "$err_file" >&2 2>/dev/null || true
    warn "  Check the exact syntax for your version: ${TS_BIN} serve --help"
    warn "  Membro is unaffected: it keeps serving on http://127.0.0.1:${PORT}."
    exit 0
  fi
fi

# ---- verify: read back the live route, refuse to proceed if Funnel is on ----
route_status="$("$TS_BIN" serve status 2>&1 || true)"
ok "$route_status"

if printf '%s\n' "$route_status" | grep -qi "funnel on"; then
  warn "REFUSING: Funnel appears to be ON for this device. Membro must never be"
  warn "  reachable off your tailnet. Run: ${TS_BIN} funnel off"
  exit 1
fi

# ---- report the resulting URL, never a secret ----
dns_name="$("$TS_BIN" status --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("Self", {}).get("DNSName", "").rstrip("."))
except Exception:
    pass
' 2>/dev/null || true)"

if [ -n "$dns_name" ]; then
  ok "Membro reachable on your tailnet at: https://${dns_name}:${TS_PORT}/"
  ok "To sign in from a browser there, that hostname must also be named in"
  ok "MEMORY_TRUSTED_HOSTS (#83) — otherwise the request is refused by design:"
  ok "  MEMORY_TRUSTED_HOSTS=${dns_name}"
else
  ok "Route configured/verified. Run '${TS_BIN} status' for your device's"
  ok "tailnet hostname, then browse to https://<that hostname>:${TS_PORT}/"
  ok "That hostname must also be in MEMORY_TRUSTED_HOSTS to sign in (#83)."
fi
