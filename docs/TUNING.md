# Tuning: every knob, what it does, and why you'd turn it

This service works out of the box with no tuning at all. But memory is
personal, and the defaults are best guesses rather than laws; this page lists
everything you can change, in plain English, with the reasoning behind each
default. It is written to be pasted at an AI assistant ("help me set this up
for my situation") or followed unaided.

## How to change a setting

Settings are read in this order (later wins):

1. Built-in defaults (listed below)
2. `config.json` in the repo root: settings you'd share
3. `config.local.json`: personal overrides, gitignored
4. Environment variables (a few operational ones, noted below)

Create `config.local.json` next to `start.sh` with just the keys you want to
change, e.g.:

```json
{"user_name": "Alex", "memory_summary_words": 1500}
```

Restart the service (`./start.sh`) to apply.

**Experimenting is safe.** Summary rebuilds are versioned (admin page →
Summary → Previous versions; restore anything), facts are never deleted by
the software, and the database is snapshotted automatically. Change a knob,
regenerate, read the result; restore if you liked the old one better.

## The profile (summary): what your AIs read every round

| Setting | Default | What it does |
|---|---|---|
| `memory_summary_words` | `2000` | Word budget for the profile |
| `summary_model` | `claude-sonnet-5` | Which model writes it |
| `user_name` | `"User"` | Your name, used in the profile and prompts |

**`memory_summary_words`** is the profile's word budget, enforced by
rewriting rather than truncation: a draft that comes back meaningfully over
budget gets one "compress to budget" rewrite, so the newest sections are
never cut off (the exact tolerance and what happens if the rewrite fails
are in [MEMORY_DESIGN.md](MEMORY_DESIGN.md)). A bigger budget is not
automatically better: the profile competes with your actual conversation
for the model's attention, and the ledger remains fully searchable for
anything the profile omits. Raise it if models keep having to look up
everyday context; lower it if replies feel like they're reciting your
biography. The admin page shows actual words next to budget.

**`summary_model`**: the profile is the most-read document in the system
(every model, every round) and rebuilds are rare, so it defaults to a
stronger model than the fact miner. Any Anthropic (`claude-*`) or OpenAI
model name works; the needed API key must be set.

**The profile's headings are not a setting** (they used to be). The profile
keeps a fixed spine (Identity, Preferences, Relationships & People at the
top; Goals & Active Threads, Recent Changes at the bottom) and the model
names the two to five middle sections from what your facts actually cluster
around ("Pottery", "The garden build"…). Topics appear when they earn space
and dissolve as they fade; why the spine is fixed is in
[MEMORY_DESIGN.md](MEMORY_DESIGN.md). This ran behind a `summary_emergent_topics`
flag from 2026-07-09 and graduated on 2026-07-26, so there is no fixed-headings
mode any more; an old value left in your config is simply ignored.

**Importance and permanence.** The miner scores the facts it extracts
from conversations 1–9: how much a fact matters to understanding you
long-term. Facts you save by hand, facts saved by an external tool, and
imported ones arrive **unscored**: they show no score in the ledger
editor and count as a neutral 5 for both ageing and profile selection
until you set a score yourself (admin page → Ledger → Edit). Higher
importance ages slower and holds profile space longer. **Importance 10 is
yours alone**: it marks a fact PERMANENT, so it never decays and is always in
the profile, regardless of age or how full the ledger gets. Use it for the
handful of facts that are true until explicitly changed (a spouse's name, a
child's birthday). The miner is capped at 9, so nothing automated can mint
permanence. The system still does the finding for you: the consolidation
sweep (admin page → Run sweep) nominates permanent-looking facts among your
high-importance cards, each with a one-click **Pin**. The sweep only
proposes; granting permanence is always your action.

## The miner: what gets remembered from conversations

| Setting | Default | What it does |
|---|---|---|
| `miner_model` | `claude-haiku-4-5` | Cheap model that extracts facts from chats |
| `trusted_apps` | `[]` | Apps whose saves skip quarantine; empty means none |
| `grounding_allowlist` | `[]` | Proper nouns the walls should never question |

**`miner_model`** runs often (after every chat), so it defaults cheap. If
mined facts feel off, a stronger model here helps, at real cost, since it
reads whole conversations.

**`trusted_apps`** is empty by default, so out of the box *nothing* is
app-trusted: every write that isn't the literal `user` origin is quarantined
for your review. Add your own client's app slug here only once you're
satisfied its saves belong in canon unreviewed. An `mcp:*` origin is never
trusted whatever this is set to.

**`grounding_allowlist`**: the extraction walls quarantine facts containing
proper nouns that never appeared in the source conversation (contamination
defence). If a name is ubiquitous in *your* life (your employer, your town),
add it here so it's never flagged. This lives in config rather than code
deliberately, because which nouns are ubiquitous differs for every owner.

## Recall & embeddings

| Setting | Default | What it does |
|---|---|---|
| `embedding_model` | `text-embedding-3-small` | Vectors for semantic recall |

Semantic recall needs an OpenAI key; without one, recall degrades to
keyword-only (still works, finds less). Deeper recall constants (the
semantic floor, paraphrase cutoff, recency weight, decay half-life) are
set in code rather than config; they're documented in
[MEMORY_DESIGN.md](MEMORY_DESIGN.md) and on the Mathematics page, where
you can watch them act on your real data. One consequence worth knowing
before you go looking for a knob: a recall can come back with fewer
facts than the limit asked for. Cards that clear neither the similarity
floor nor a word from the query never enter the ranking, and
near-duplicates collapse after it; returning fewer honest cards is
preferred to padding an answer with noise. If live use convinces you one
of these constants is wrong, that's a bug report we want.

## Storage, backups & serving

| Setting | Default | What it does |
|---|---|---|
| `data_dir` | `./data` | Where the database lives (env: `MEMORY_DATA_DIR`) |
| `backup_interval_hours` | `6` | Automatic snapshot cadence (env available, `0` disables) |
| `backup_keep` | `14` | Local snapshots kept |
| `mirror_dir` | unset | Second folder (e.g. iCloud) for snapshot copies (env available) |
| `mirror_keep` | `7` | Mirrored snapshots kept |
| `host` / `port` | `127.0.0.1` / `8901` | Loopback-only by default, the security model (env: `MEMORY_PORT`) |
| `auth_token` | unset | Your owner recovery secret and machine credential (env: `MEMORY_AUTH_TOKEN`) |
| `trusted_hosts` | empty | Hostnames (your own tailnet name) where you may sign in with your owner password from a browser; the ledger stays sealed without a credential either way. Config key `trusted_hosts` (a JSON list), or env `MEMORY_TRUSTED_HOSTS` (comma-separated). Read [SECURITY.md](../SECURITY.md) first |
| `MEMORY_TAILSCALE_SERVE` | `0` (off) | Opt in to tailnet-only remote access at startup (env only, see below) |
| `MEMORY_TAILSCALE_PORT` | `8443` | The HTTPS port Membro gets on your tailnet; its own origin rather than a path (env only) |
| `MEMORY_TAILSCALE_BIN` | unset | Path to a `tailscale` CLI that is installed but not on `PATH`, the normal macOS case (env only) |

Set `mirror_dir` to a folder inside iCloud Drive/Dropbox/OneDrive and every
snapshot is copied off-machine automatically. This is strongly recommended,
because the database holds your accumulated memory.

**`auth_token`** is one secret with three jobs: (1) proof that you're the
owner when you set or reset your admin password, (2) the
`Authorization: Bearer` credential for machine callers (MCP servers
such as the optional `membro-admin`, or curl), and (3) the thing that
lets the service be served beyond localhost at all: with no configured
value it refuses to bind a non-loopback host. Leaving it unset does
**not** mean no secret exists: the service mints a fresh random one at
every start and prints it to the terminal that ran `./start.sh`. Set a
stable value in `.env` or `config.local.json` if you register the
`membro-admin` MCP server, or if you simply don't want your recovery
secret changing on every restart. An auto-minted token still only works
locally; remote serving needs a configured one.

**Logging in.** The exact-row parts of the ledger always require an
owner credential, even on loopback, so another process sharing
127.0.0.1 can't read or edit those. That covers raw facts, the review
queue, every action on a single fact (edit, supersede, approve,
dismiss, delete), the bulk quarantine and dismiss-all verbs, verbatim
transcript search, the attachment routes, the consolidation sweep, and
job results. It does **not** cover everything: recall, the profile,
health, backup, the visualisation feeds, the stored profile versions
(including restoring one), and regenerating the live profile all
answer a local caller with no credential. [SECURITY.md](../SECURITY.md) says which, and why.
Two credentials satisfy the gate, and only one of them is for everyday
use:

- **Your password** (the browser). On first run,
  `http://127.0.0.1:8901` shows a setup form: paste the recovery secret
  from the terminal to prove it's you, then choose a password (8
  characters minimum). After that you just log in with the password;
  it's durable across restarts. Forgotten it? "Forgot your password?"
  on the same page takes the recovery secret again and sets a new one.
  A successful login sets a private, HttpOnly cookie holding an opaque
  session id, never the token itself. It lasts 24 hours, "Log out"
  revokes it instantly and everywhere, and a restart clears every
  session. That duration is fixed in code; there is no setting for it.
- **The token** (MCP and curl). Machine callers send
  `Authorization: Bearer <auth_token>` and never touch the password.

Your password is stored only as a salted scrypt verifier, never in a
reversible form, and never handed back to the browser. The endpoints are
in [API.md](API.md), the threat model and its honest limits in
[SECURITY.md](../SECURITY.md), and the first-run walkthrough in
[README.md](../README.md).

**Remote access over your tailnet.** `MEMORY_TAILSCALE_SERVE=1`
in `.env` makes `start.sh` run `scripts/tailscale-serve.sh` at every startup,
which maps this loopback service onto its own HTTPS port on your OWN
Tailscale tailnet (`MEMORY_TAILSCALE_PORT`, default `8443`) via `tailscale
serve`; never `tailscale funnel`, never a public port (see SECURITY.md's
trust boundary). It uses a dedicated port rather than a path under the
tailnet root on purpose: the admin UI links absolute paths (`/math`,
`/v1/…`), which under a `/membro` prefix would resolve to the root and hit
whatever else is served there; on a machine also serving the chat client,
that is a different application entirely. To actually sign in from a browser
you must ALSO name that hostname in `MEMORY_TRUSTED_HOSTS`, or the request
is refused by design.
Off by default; nothing changes unless you set it. Requires the Tailscale CLI
and `tailscale up` already run on this machine. **On macOS the CLI is normally
installed but not on `PATH`**: the Mac App Store Tailscale app bundles a
working CLI that supports `serve`, so rather than installing a
second Tailscale, point the script at it:
`MEMORY_TAILSCALE_BIN=/Applications/Tailscale.app/Contents/MacOS/Tailscale`.
Loopback binding and your existing owner password / `MEMORY_AUTH_TOKEN` are
completely unchanged: the tailnet is an additional private network path to
the same authenticated service rather than a new credential. If Tailscale
isn't installed or isn't signed in, startup prints a loud but non-blocking
warning and Membro keeps serving on loopback exactly as before. Re-run it
any time with `bash scripts/tailscale-serve.sh` (or `--status` to just
check, changing nothing); it's idempotent and safe to repeat.

## What is deliberately NOT tunable

- **Append-only**: no setting turns off "facts are never deleted by
  software." The only hard delete is you, in the admin UI.
- **The write gate**: external (MCP) saves are always quarantined for your
  review. No trust setting bypasses it.
- **The episodic record**: ingested messages are immutable.

These are the invariants the rest of the system's honesty depends on
(tested in `tests/test_invariants.py`).
