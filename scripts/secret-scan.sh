#!/usr/bin/env bash
# Leak scan — ONE implementation for the pre-commit hook AND CI, so the two
# can never drift. It rejects two DISTINCT classes of leak (see docs/COLLABORATOR_SAFETY.md):
#
#   1. Secrets  — API-key SHAPES (prefix + a long body). A secret is a key you
#      rotate: catch it, revoke it, move on.
#   2. Personal / infrastructure IDENTIFIERS — a real Tailscale host
#      (`<host>.ts.net`), a machine-specific home path (`/Users/<name>`,
#      `/home/<name>`), or a personal email. An identifier is a FACT you cannot
#      un-publish once it lands on a public remote; the only real fix is to keep
#      it out. These aren't credentials, so a secrets-only scan had nothing to
#      match — that gap is exactly why this class was added.
#
# Documented placeholders pass (my-mac.my-tailnet.ts.net, <mac>.<tailnet>.ts.net,
# tailXXXX examples, /Users/you, /home/you, you@example.com, GitHub noreply,
# git@github.com) so docs keep worked examples. The scanner itself, the hook, the
# identifier fixtures, and requirements.txt are excluded (no self-match, no
# hash/version false positives).
#
# Three modes, one rule set:
#   (default)         staged added lines   — the pre-commit hook
#   --tree            the whole committed tree — CI (test_secret_scan.py)
#   --files a b c     specific files       — tests / ad-hoc
#
# Run manually:  bash scripts/secret-scan.sh          # staged
#                bash scripts/secret-scan.sh --tree    # whole tree
# Enable the hook once:  git config core.hooksPath .githooks
set -uo pipefail

# ── real-key SHAPES: a prefix followed by enough body to be an actual credential
#    (bare prefixes like "sk-ant" mentioned in docs must NOT trip this).
secret_patterns='sk-ant-[A-Za-z0-9_-]{24,}|sk-proj-[A-Za-z0-9_-]{24,}|sk-[A-Za-z0-9]{40,}|tvly-(dev-)?[A-Za-z0-9_-]{16,}|BSA[A-Za-z0-9_-]{20,}|sk_[a-f0-9]{32,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9-]{12,}'

# ── identifier TOKEN shapes (extracted, then filtered against the allowlists below)
ts_token='[A-Za-z0-9<>_.-]+\.ts\.net'
home_token='/(Users|home)/[A-Za-z0-9._<>-]+'
email_token='[A-Za-z0-9][A-Za-z0-9._%+<>-]*@[A-Za-z0-9.<>-]+\.[A-Za-z]{2,}'

# ── placeholder allowlists: a matched token is DROPPED if it contains one of these
#    markers (documented, synthetic examples only).
allow_ts='tail[Xx]{3,}|my-tailnet|<[a-z]+>|example'                       # real tailnet names have none of these
allow_home='^/(Users|home)/(you|USER|username|user|name|me|<[a-z]+>)$'    # only placeholder home names pass
allow_email='@example\.(com|org|net)$|@users\.noreply\.github\.com$|^git@github\.com$|^noreply@|<[a-z]+>'

# Paths whose CONTENT is exempt (this scanner, the hook, the fixtures that must
# contain real-looking strings to be tested, and the version-pinned requirements).
excludes=(
  ':(exclude)scripts/secret-scan.sh'
  ':(exclude).githooks/pre-commit'
  ':(exclude)tests/fixtures/identifiers/*'
  ':(exclude)tests/test_secret_scan.py'
  ':(exclude)requirements.txt'
)

mode="staged"
files=()
case "${1:-}" in
  --tree)  mode="tree" ;;
  --files) mode="files"; shift; files=("$@") ;;
  "")      mode="staged" ;;
  *)       echo "usage: secret-scan.sh [--tree | --files <paths...>]" >&2; exit 2 ;;
esac

# Emit the text to be scanned on stdout, one logical unit per line.
collect() {
  case "$mode" in
    staged)
      git diff --cached -U0 -- . "${excludes[@]}" \
        | grep -E '^\+' | grep -vE '^\+\+\+' || true
      ;;
    tree)
      # Every tracked, non-excluded, non-binary file's content.
      git ls-files -z -- . "${excludes[@]}" \
        | while IFS= read -r -d '' f; do
            [ -f "$f" ] || continue
            grep -Iq . "$f" 2>/dev/null || continue   # skip binary files
            cat "$f"
          done
      ;;
    files)
      for f in "${files[@]}"; do
        [ -f "$f" ] || continue
        cat "$f"
      done
      ;;
  esac
}

text="$(collect)"

# ── class 1: secrets
secret_hits="$(printf '%s\n' "$text" | grep -nE "$secret_patterns" || true)"

# ── class 2: identifiers (extract candidate tokens, drop allowlisted placeholders)
ts_hits="$(printf '%s\n' "$text"    | grep -oE "$ts_token"    | grep -viE "$allow_ts"    || true)"
home_hits="$(printf '%s\n' "$text"  | grep -oE "$home_token"  | grep -viE "$allow_home"  || true)"
email_hits="$(printf '%s\n' "$text" | grep -oE "$email_token" | grep -viE "$allow_email" || true)"
id_hits="$(printf '%s\n%s\n%s\n' "$ts_hits" "$home_hits" "$email_hits" | grep -vE '^[[:space:]]*$' || true)"

# ── class 3: personal content, patterns from the gitignored local deny-list.
#    The list itself is personal data, so it never ships — CI (no file) skips
#    this class and says so. Overridable for the scanner's own tests.
local_list="${SECRET_SCAN_LOCAL:-$(git rev-parse --show-toplevel 2>/dev/null || echo .)/.secret-scan-local}"
personal_hits=""
personal_count=0
if [ -f "$local_list" ]; then
  while IFS= read -r pat; do
    case "$pat" in ''|'#'*) continue ;; esac
    personal_count=$((personal_count + 1))
    hit="$(printf '%s\n' "$text" | grep -inE "$pat" | head -5 || true)"
    [ -n "$hit" ] && personal_hits="${personal_hits}${personal_hits:+
}  pattern '$pat':
$(printf '%s\n' "$hit" | sed 's/^/    /')"
  done < "$local_list"
fi

status=0

if [ -n "$secret_hits" ]; then
  echo "✖ secret-scan: a possible real KEY is in scope —"
  printf '    %s\n' "$secret_hits"
  echo "  Keys belong in .env (gitignored). Rotate it if it was ever committed."
  status=1
fi

if [ -n "$id_hits" ]; then
  echo "✖ secret-scan: a personal / infrastructure IDENTIFIER is in scope —"
  printf '    %s\n' "$id_hits"
  echo "  Real Tailscale hosts, home paths, and personal emails must not ship."
  echo "  Use a placeholder (my-mac.my-tailnet.ts.net, /Users/you, you@example.com)."
  status=1
fi

if [ -n "$personal_hits" ]; then
  echo "✖ secret-scan: PERSONAL CONTENT matched the local deny-list —"
  printf '%s\n' "$personal_hits"
  echo "  These patterns are personal facts (.secret-scan-local, gitignored)."
  echo "  Replace the content with a synthetic equivalent — do not weaken the list."
  status=1
fi

if [ "$status" -ne 0 ]; then
  echo
  echo "  If a hit is a genuine false positive, commit with: git commit --no-verify"
  echo "  and add the placeholder to scripts/secret-scan.sh's allowlist."
  exit 1
fi

if [ "$personal_count" -gt 0 ]; then
  echo "✓ secret-scan clean (key shapes + infra identifiers + $personal_count local deny-list patterns — NOT a publication clearance)"
else
  echo "✓ secret-scan clean (key shapes + infra identifiers; no .secret-scan-local — personal-content class SKIPPED)"
fi
