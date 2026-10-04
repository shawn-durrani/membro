# Tuning: every setting, what it does, and why you'd change it

Membro works out of the box with no tuning at all. Memory is personal,
though, and the defaults are best guesses. Each setting you can change
comes with the reason for its default. You can follow along yourself,
or paste the settings into an AI assistant and ask it to help you set
Membro up for your situation.

## How to change a setting

Membro reads settings in this order, and a later one wins:

1. the built-in defaults
2. `config.json` in the repo root, for settings you'd share
3. `config.local.json`, for your own, which git ignores
4. environment variables, for the few settings that have one

Create `config.local.json` next to `start.sh`, with only the keys you
want to change. For example:

```json
{"user_name": "Alex", "memory_summary_words": 1500}
```

Then restart Membro. If you installed the supervisor, that's this
command.

```sh
launchctl kickstart -k gui/$(id -u)/dev.membro.server
```

Without the supervisor, stop Membro and run `./start.sh` again. It
won't start while another copy holds the port.

Experimenting is safe. Every profile rebuild is kept as a version, and
you can restore any of them from the admin page, under Summary and then
Previous versions. The software never deletes a fact, and the database
is snapshotted automatically. Change a setting, rebuild, read the
result, and restore the old one if you liked it better.

## The profile: what your models read every round

| Setting | Default | What it does |
|---|---|---|
| `memory_summary_words` | `2000` | The profile's word budget |
| `memory_summary_fill` | `0.8` | The share of the budget the profile should reach. `0` turns the floor off |
| `summary_model` | `claude-sonnet-5` | Which model writes the profile |
| `user_name` | `"User"` | Your name, used in the profile and the prompts |

`memory_summary_words` is the profile's word budget. It's kept by
rewriting, never by truncating. A draft that comes back well over budget
gets one pass that compresses it, so the newest sections are never cut
off. [MEMORY_DESIGN.md](MEMORY_DESIGN.md) has the exact tolerance, and
what happens if that pass fails. A bigger budget isn't automatically
better. The profile competes with your conversation for the model's
attention, and the ledger stays fully searchable for anything the
profile leaves out. Raise it if models keep having to look up everyday
context. Lower it if replies feel like they're reciting your biography.
The admin page shows the real word count next to the budget.

`memory_summary_fill` sets the floor of the target range, as a share of
the budget. With the defaults the model is asked for 1,600 to 2,000
words. It's told to spend the room on the specifics the entries carry,
like dates, the names of projects and places, numbers, and where each
thread stands. A draft that comes back under the floor gets one pass
that expands it from the entries. That pass runs only when the selected
entries hold at least twice the floor in words. A thin ledger gets a
short profile, and nothing is made up to fill the room. Set the fill to
`0` to ask for the ceiling alone, and a value over 1 counts as 1. The
admin page shows the fill as a percentage, and each stored version says
whether it was expanded or squeezed.

`summary_model` defaults to a stronger model than the miner. The
profile is the most read thing in the system, by every model in every
round, and rebuilds are rare. Any Anthropic `claude-*` model name works,
and so does an OpenAI one, as long as its key is set. Claude Sonnet 5
and newer models think before they write, and the thinking shares the
profile's room. Each build call gets 16,000 tokens, or four per word of
the budget if that's more. You pay for what the model writes, and not
for the room. If the draft still runs out, the profile you had stays. If
a later pass that expands or compresses the draft runs out, the finished
draft is kept. The service log says which happened.

The profile's headings aren't a setting. The profile keeps a fixed spine:
Identity, Preferences, and Relationships & People at the top, and Goals &
Active Threads and Recent Changes at the bottom. The model names the two
to five middle sections from what your facts cluster around, like
"Pottery" or "The garden build". Topics appear when they earn the room,
and fade as their facts fade. [MEMORY_DESIGN.md](MEMORY_DESIGN.md) says
why the spine is fixed. A `summary_emergent_topics` value left in your
config is ignored.

### Importance and permanence

The miner scores the facts it pulls from conversations from 1 to 9, for
how much each one matters to understanding you over the long run. Facts
you save by hand, facts an outside tool saves, and imported facts arrive
unscored. They show no score in the ledger editor, and count as 5 for
both fading and the profile, until you set a score yourself. You do that
on the admin page, under Ledger and then Edit. A higher score fades
slower, and holds a place in the profile longer.

Importance 10 is yours alone. It marks a fact permanent, so it never
fades and it's always in the profile, however old it is and however full
the ledger gets. Use it for the handful of facts that stay true until you
change them, like a spouse's name or a child's birthday. The miner is
capped at 9, so nothing automatic can make a fact permanent. Membro still
does the finding for you. The consolidation sweep, under Run sweep on the
admin page, suggests facts that look permanent among your high
importance cards, each with a one-click Pin. The sweep only suggests,
and making a fact permanent is always your call.

## The miner: what gets remembered from conversations

| Setting | Default | What it does |
|---|---|---|
| `miner_model` | `claude-haiku-4-5` | The cheap model that pulls facts out of chats |
| `caption_model` | empty, meaning the miner model | The model that describes images |
| `llm_base_url` | empty | An OpenAI-compatible server for model names that aren't `claude-*` |
| `judge_pass` | `false` | Turns on the judge pass over held facts |
| `judge_model` | empty, meaning the miner model | The model the judge pass uses |
| `trusted_apps` | `[]` | Apps whose saves skip the hold on where a write came from. Empty means none |
| `grounding_allowlist` | `[]` | Names the walls should never question |

`miner_model` runs after every chat, so it defaults to a cheap model. If
mined facts feel off, a stronger model helps, at a real cost, because it
reads whole conversations.

### When the model thinks, or runs out of room

Claude Sonnet 5 and newer models think before they answer, unless a
request turns it off. The thinking comes out of the same room as the
answer. The miner itself thinks on a model that does by default, because
that's how it was measured. It gets 4,000 tokens for thinking on top of
the 1,000 for its facts. On the Sonnet 5.5 benchmark its thinking never
passed about 525 tokens. The miner's two retries, captions, the sweep and
the judge's witness check answer in a fixed format. Membro turns thinking
off for them where the model allows it. On a model that always thinks,
each of those calls gets 4,000 more tokens of room. The judge's roleplay
check still thinks a little first. It gets a fixed budget on a model that
takes one, and the model's own thinking on one that doesn't.

Every call checks how its reply ended, and a reply cut short or refused
is never read as an answer. When a miner reply is cut short or refused,
the miner mines the same messages again in halves. A single message that
runs out of room gets one more try with 4,000 tokens. A message the model
still can't finish, or refuses, is left unmined. The service log names
its message number and the reason, and its words stay in the chat's
history.

### Captions, local models, the judge and trust

`caption_model` describes images, so search and the miner can see what's
in them. Empty means the miner model. Set it only if your miner can't
read images, because captions need a model that can.

`llm_base_url` points every model name that isn't `claude-*` at a server
that answers the way OpenAI's API does, such as
[Ollama](https://ollama.com)'s `http://127.0.0.1:11434/v1`. With it set,
those models need no API key. Each model setting is routed by its own
name, so a local miner can sit beside a cloud `summary_model`, or replace
it too. Nothing leaves your computer for mining, captions or summaries
only once `miner_model`, `caption_model` and `summary_model` all name
local models. Pick a server that's always on. If it's down, mining fails
loudly and tries again later, and nothing is lost.

`judge_pass` turns on the judge pass. It's a background sweep over held
facts that runs at startup and then every hour. It can release a fact
held by grounding, but only when the model quotes the missing name from
the chat word for word. It can also move a fact held as roleplay into a
review group of its own, when it judges the chat was technical, so you
can clear that group at once. It never releases a roleplay hold. It looks
at each fact at most once a day, and any failure leaves the fact as it
was. `judge_model` picks the model it uses.

`trusted_apps` is empty by default, so out of the box no app is trusted.
Every write that isn't the literal `user` origin is held for your review.
Add your own client's app name here only once you're happy its saves
belong in the trusted facts without review. A trusted app skips only the
hold on where a write came from. A save is still held when its round read
the web or a guest was in the room, and mined facts still go through the
walls. An `mcp:*` origin is never trusted, whatever this list says.

`grounding_allowlist` names the walls should never question. The walls
hold a fact whose names never appeared in the chat it came from, to stop
contamination. If a name is everywhere in your life, like your employer
or your town, add it here so it's never flagged. It lives in config, and
not in code, because the names that are everywhere differ for every
owner.

### What a mining call costs

Each mining call sends the miner's instructions, a list of your existing
facts and the new part of the chat. The instructions and the list are
laid out to stay the same from one call to the next. When calls come in a
burst, such as a long backlog or an import, each one after the first
reads that shared part from Anthropic's
[prompt cache](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)
at a tenth of the price. A call on its own, minutes after the last one,
doesn't ask for the cache. An entry lives five minutes and costs a
quarter more to write, so it would expire unread. Claude Haiku 4.5
caches only a shared part of 4,096 tokens or more. The list reaches
that at somewhere between 40 and 100 facts, depending on how long they
are.

A profile build works the same way. Its expansion pass reads the entries
its draft sent a few seconds earlier.

Every call to a chat model writes one line to the service's error output.
Under the launchd supervisor that lands in `data/service.log`, and from
`./start.sh` it's your terminal. The line names the call site, such as
`miner` or `summary.draft`, and the model. It gives the tokens sent
fresh, written to the cache, read from it and written out, and never any
text. It ends with that site's number of calls, and its share of input
read from the cache, since the service started. Embedding calls write no
line.

## Claude Code sessions

| Setting | Default | What it does |
|---|---|---|
| `claude_code_feed` | `false` | Keeps a short note of each Claude Code session |
| `claude_code_dir` | empty, meaning Claude Code's own folder | Where the session files are |
| `claude_code_quiet_minutes` | `30` | How long a session sits untouched before it's read |
| `claude_code_model` | empty, meaning the miner model | The model that writes each note |

`claude_code_feed` turns the feed on. Every five minutes Membro looks
through Claude Code's session files for one that's changed and gone
quiet. A model writes a few sentences about the new part, saying what
you set out to do, what got done and what's left open. The note is kept as a
chat from the `claude-code` app, under the session's own id, and dated
to the last turn it covers. It's mined straight away, and the profile is
rebuilt once a pass when a note gave a new fact. A pass writes at most
ten notes, so the first one over a long history spreads across a few
passes. A note costs about what mining a chat of that length costs.

The model sees the prompts you typed and the replies Claude wrote, each
trimmed. Tool calls and their output aren't sent. Before anything goes,
Membro masks whatever looks like a key, a token or a password, and the
note goes through the same mask on its way back. A session started
through the SDK, like an app's scripted run, is skipped. Subagents
keep their own files, and those aren't read either.

`claude_code_dir` defaults to `~/.claude/projects`, or the `projects`
folder under `CLAUDE_CONFIG_DIR` if you've set that.
`claude_code_quiet_minutes` is how long a session sits untouched before
it's read. A shorter wait gets notes in sooner, but a session you come
back to gets one note per stretch of work. Facts from a note are trusted
only as far as `claude-code` is, so add it to `trusted_apps` to have
them judged by the walls alone. Membro tracks how far it's read each
session in a table of its own, so a note you erase stays erased.

## Recall and embeddings

| Setting | Default | What it does |
|---|---|---|
| `embedding_model` | `text-embedding-3-small` | Vectors for recall by meaning |
| `embedding_base_url` | empty, meaning OpenAI | A server for embeddings that answers the way OpenAI's API does |

Recall by meaning needs an OpenAI key, or a server named in
`embedding_base_url`. Without either, recall matches keywords only. It
still works, and finds less. `embedding_base_url` is the same choice as
`llm_base_url`, for memory search. It's kept separate because chat and
embeddings are often served by different servers. Changing
`embedding_model` is safe but not free. Every stored vector is dropped
and rebuilt in the background, and search runs on keywords alone until
the rebuild finishes. Vectors from different models are never compared.

The deeper recall constants are set in code, and not in config. They're
the similarity floor, the paraphrase cutoff, the weight on recency, and
how fast cards fade. [MEMORY_DESIGN.md](MEMORY_DESIGN.md) describes
them, and the Mathematics page shows them acting on your real data.

A recall can come back with fewer facts than the limit asked for. Cards
that clear neither the similarity floor nor a word from the question
never enter the ranking, and near duplicates collapse after it. A few
accurate cards beat an answer padded with noise. If real use convinces
you one of these constants is wrong, that's a bug report we want.

## Storage, backups and serving

| Setting | Default | What it does |
|---|---|---|
| `data_dir` | `./data` | Where the database lives. Env `MEMORY_DATA_DIR` |
| `backup_interval_hours` | `6` | Hours between automatic snapshots, by the clock, so time asleep counts. `0` turns them off. Env `MEMORY_BACKUP_INTERVAL_HOURS` |
| `backup_keep` | `14` | Local snapshots kept |
| `mirror_dir` | unset | A second folder, like one in iCloud Drive, for a copy of each snapshot. Env `MEMORY_MIRROR_DIR` |
| `mirror_keep` | `7` | Mirrored snapshots kept |
| `host` and `port` | `127.0.0.1` and `8901` | Loopback only by default, which is the security model. Env `MEMORY_PORT` |
| `auth_token` | unset | Your owner recovery secret and machine credential. Env `MEMORY_AUTH_TOKEN` |
| `trusted_hosts` | empty | Names, like your own tailnet name, where a browser may sign in with your password. The ledger stays sealed without a credential either way. A JSON list, or env `MEMORY_TRUSTED_HOSTS` separated by commas. Read [SECURITY.md](../SECURITY.md) first |
| `browser_origin` | empty, worked out for you | The address a browser can open Membro at, reported on `/v1/health`. Env `MEMORY_BROWSER_ORIGIN` |
| `tailscale_port` | `8443` | The HTTPS port Membro gets on your tailnet, as its own address. Env `MEMORY_TAILSCALE_PORT` |
| `MEMORY_TAILSCALE_SERVE` | `0`, off | Turns on access from your tailnet at startup. Env only |
| `MEMORY_TAILSCALE_BIN` | unset | The path to a `tailscale` command that's installed but not on `PATH`. Env only |
| `sibling_apps` | Crossband, Spendglass and Threadfold on their usual ports | Your other apps, linked from the admin page's header. Env `MEMORY_SIBLING_APPS`, as JSON. `{}` turns the row off |

Set `mirror_dir` to a folder inside iCloud Drive, Dropbox or OneDrive, and
every snapshot is copied off the computer automatically. We strongly
recommend it, because the database holds your accumulated memory.

When `browser_origin` is empty, Membro works it out. It's
`https://<first trusted host>:<tailscale_port>` when a browser may sign
in from a tailnet name, and loopback on `port` otherwise.
`scripts/tailscale-serve.sh` reads the port only from
`MEMORY_TAILSCALE_PORT`, so set it there if you change it.

### The token

`auth_token` is one secret, and it does all of these jobs.

1. It proves you're the owner when you set or reset your admin password.
2. It's the `Authorization: Bearer` credential for machine callers, like
   MCP servers such as the optional `membro-admin`, or `curl`.
3. It lets the service be served beyond localhost at all. With no
   configured value, Membro refuses to bind an address off loopback.

Leaving it unset doesn't mean there's no secret. The service makes a
fresh random one at every start. It prints it only on a first run,
before you've set a password, because that's the one time you need it.
Set a stable value if you register the `membro-admin` MCP server, or if
you might ever need to reset a forgotten password. That's
`MEMORY_AUTH_TOKEN` in `.env`, or `auth_token` in `config.local.json`. A
configured value is never printed. A token Membro made itself works only
on loopback, and serving remotely needs a configured one.

### Signing in

Reading or changing the exact rows of the ledger always needs an owner
credential, even on loopback, so another program on 127.0.0.1 can't read
or edit them. Recall, the profile and a few other routes answer a local
caller with no credential. [SECURITY.md](../SECURITY.md) lists which, and
why. A browser signs in with your password or a passkey, and a machine
caller with the token.

- Your password. On a first run, `http://127.0.0.1:8901` shows a setup
  form. Paste the recovery secret to prove it's you. That's your
  `MEMORY_AUTH_TOKEN`, or the one the first start printed. Then choose a
  password of at least 8 characters. From then on you sign in with the
  password, and it lasts across restarts. If you forget it, "Forgot your
  password?" on the same page takes the recovery secret again and sets a
  new one.
- A passkey. Once you're signed in, you can enrol a passkey, and the
  lock screen offers it first. A passkey works at `localhost` or a
  trusted host, and never at an address like `127.0.0.1`.
- The token, for MCP and `curl`. Machine callers send
  `Authorization: Bearer <auth_token>` and never touch the password.

Signing in sets a private, HttpOnly cookie that holds an opaque session
id, never the token itself. It lasts 24 hours and survives a restart.
Log out ends it at once, everywhere. A password reset or a passkey
removal signs out every other browser, and a restore signs out everyone.
The 24 hours is fixed in code, and there's no setting for it.

Your password is stored only as a salted scrypt verifier, never in a form
that can be reversed, and it's never sent back to the browser. The
endpoints are in [API.md](API.md), the threat model and its limits in
[SECURITY.md](../SECURITY.md), and the first run in
[README.md](../README.md).

### Remote access over your tailnet

Set `MEMORY_TAILSCALE_SERVE=1` in `.env`, and `start.sh` runs
`scripts/tailscale-serve.sh` at every start. The script uses
[`tailscale serve`](https://tailscale.com/docs/features/tailscale-serve)
to put this loopback service on its own HTTPS port on your own tailnet.
That's `MEMORY_TAILSCALE_PORT`, 8443 by default. It never uses
`tailscale funnel`, and never a public port, as the trust boundary in
[SECURITY.md](../SECURITY.md) explains. To sign in from a browser on the
tailnet, also name that host in `MEMORY_TRUSTED_HOSTS`, or the request
is refused.

Membro gets a port of its own, and not a path under the tailnet's root.
The admin page links absolute paths, like `/math` and `/v1/…`. Under a
`/membro` prefix those would resolve to the root, and reach whatever else
is served there. On a computer that also serves the chat app, that's a
different app entirely.

It's off by default, and nothing changes unless you set it. It needs the
Tailscale command line tool, with `tailscale up` already run on this
computer. If you installed Tailscale from the Mac App Store, the tool
comes inside the app, isn't on `PATH`, and supports `serve`. Point the
script at it, and don't install a second Tailscale.

```sh
MEMORY_TAILSCALE_BIN=/Applications/Tailscale.app/Contents/MacOS/Tailscale
```

Loopback binding, your owner password and `MEMORY_AUTH_TOKEN` don't
change. The tailnet is one more private path to the same signed-in
service, and no new credential. If Tailscale isn't installed or isn't
signed in, startup prints a loud warning and carries on, and Membro
keeps serving on loopback as before.

The script doesn't read `.env` itself, because `start.sh` loads it
first. To run the script again by hand, load `.env` yourself, so it sees
your settings. Add `--status` to check the route without changing
anything. It's safe to run as often as you like.

```sh
set -a; . ./.env; set +a; bash scripts/tailscale-serve.sh
```

### Links to your other apps

A row at the top of the admin page links your other apps, one tap each.
Each entry in `sibling_apps` names an app, and the health address it
answers on this machine. Membro asks each one where a browser can open
it, and keeps the answers for a minute. An app that doesn't
answer within a second is left out. On the Mac every running app shows,
at its local address. From your phone, only the apps served on your
tailnet show, so the row never offers a link that won't open. Membro
reports its own address the same way, as `browser_origin` on
`/v1/health`.

## What you can't turn off

- Facts are only added to. No setting turns off the rule that the
  software never deletes a fact. The only hard deletes are yours: an
  eraser on the admin page, and a restore, which replays the erasures
  you already made. A capture app can also delete a voice clip its own
  voice bank has dropped.
- The write gate. External saves, over MCP, are always held for your
  review, and no trust setting gets around it.
- The tapes. Ingested messages never change.

The rest of the system depends on these, and `tests/test_invariants.py`
tests them.
