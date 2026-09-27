"""The throwaway services' environment, built from nothing.

Copying ``os.environ`` and overriding a few keys isn't safe here. An inherited
``MEMORY_MIRROR_DIR`` would copy throwaway snapshots into your real offsite
mirror, an inherited ``GITHUB_TOKEN`` could write to real repositories, and
membro ignores an empty override, so a blank value can't switch anything off.
So each child starts empty and gains only:

* the few process basics Python needs,
* the two model keys, by name, from the harness's own environment,
* the values the launcher forces for this run.

Anything not named here doesn't exist in the child. Forgetting to exclude
something can't leak it. The worst case is a missing variable, which fails
loudly. ``.env`` is never read by this package.
"""

from __future__ import annotations

import os

from ..safety import DISPOSABLE_ACK_ENV

# Membro's miner and summary want the Anthropic key and its embeddings want
# the OpenAI key. Crossband's seats answer with them.
CREDENTIAL_PASSTHROUGH = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")

# Never in a child. GITHUB_TOKEN can write to real repositories. The search,
# voice and Reddit keys would let a seat answer from the web without touching
# memory, which would measure the wrong thing.
EXCLUDED_CREDENTIALS = (
    "GITHUB_TOKEN", "TAVILY_API_KEY", "BRAVE_API_KEY", "ELEVENLABS_API_KEY",
    "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "CLAUDE_CODE_OAUTH_TOKEN",
)

# Instance identity. The launcher forces these or leaves them unset, and the
# guard below refuses any that turn up unforced.
NEVER_INHERIT = (
    "MEMORY_DATA_DIR", "MEMORY_PORT", "MEMORY_AUTH_TOKEN", "MEMORY_MIRROR_DIR",
    "MEMORY_TRUSTED_HOSTS", "MEMORY_TAILSCALE_SERVE", "MEMORY_SIBLING_APPS",
    "MEMORY_BROWSER_ORIGIN", "MEMORY_BACKUP_INTERVAL_HOURS",
    "CROSSBAND_DATA_DIR", "CROSSBAND_PORT", "CROSSBAND_HOST",
    "CROSSBAND_MEMORY_URL", "CROSSBAND_BACKUP_MIRROR_DIR",
    "CROSSBAND_TRUSTED_HOSTS", "CROSSBAND_INGEST_TOKEN",
    "CROSSBAND_RECOVERY_SECRET", "CROSSBAND_SIBLING_APPS",
    "CROSSBAND_MCP_SERVERS", "CROSSBAND_CODE_MCP", "CROSSBAND_CODE_REPOS",
    "CROSSBAND_GITHUB_REPOS",
    # Where the model SDKs send requests: always the harness's meter.
    "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
)

_PROCESS_BASICS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


class EnvContract(RuntimeError):
    """An assembled child environment would break the allowlist."""


def build_child_env(*, forced: dict, home: str, credentials: dict,
                    source_env: dict | None = None) -> dict[str, str]:
    """A child environment holding only the basics, the keys and ``forced``.

    ``credentials`` names the key values to pass, already chosen by the
    caller: the real keys for a paid run, placeholders for a mock one.
    ``home`` is a throwaway HOME, so any cache or config the child writes
    lands in the throwaway tree.
    """
    source = os.environ if source_env is None else source_env
    env = {name: source[name] for name in _PROCESS_BASICS if name in source}
    env["HOME"] = home
    env["TMPDIR"] = os.path.join(home, "tmp")
    for name in CREDENTIAL_PASSTHROUGH:
        if credentials.get(name):
            env[name] = credentials[name]
    env.update({k: str(v) for k, v in forced.items()})

    leaked = [n for n in EXCLUDED_CREDENTIALS if n in env]
    if leaked:
        raise EnvContract(f"child env would carry excluded credentials {leaked}")
    unforced = [n for n in NEVER_INHERIT if n in env and n not in forced]
    if unforced:
        raise EnvContract(f"child env would carry unforced identity vars {unforced}")
    return env


def membro_env(*, data_dir: str, port: int, home: str, auth_token: str,
               meter_prefix: str, credentials: dict,
               source_env: dict | None = None) -> dict[str, str]:
    """The throwaway membro's environment.

    ``meter_prefix`` is the meter's address plus this question's key. Model
    calls go to ``<prefix>.membro/<provider>``, so the meter can tell who
    sent each one.

    Its own random ``MEMORY_AUTH_TOKEN`` opens the owner-gated routes the
    harness and crossband use: the job polls, ``/v1/search`` and the fact
    counts. ``MEMORY_SIBLING_APPS={}`` keeps it from asking your real apps
    for their addresses. Mirror, trusted hosts and Tailscale are left unset,
    and the worktree carries no config naming them.
    """
    return build_child_env(
        forced={
            "MEMORY_DATA_DIR": data_dir,
            "MEMORY_PORT": str(port),
            "MEMORY_AUTH_TOKEN": auth_token,
            "MEMORY_SIBLING_APPS": "{}",
            "ANTHROPIC_BASE_URL": f"{meter_prefix}.membro/anthropic",
            "OPENAI_BASE_URL": f"{meter_prefix}.membro/openai/v1",
            DISPOSABLE_ACK_ENV: "1",
        },
        home=home, credentials=credentials, source_env=source_env)


def crossband_env(*, data_dir: str, port: int, memory_url: str, home: str,
                  memory_token: str, meter_prefix: str, credentials: dict,
                  anthropic_model: str | None = None,
                  source_env: dict | None = None) -> dict[str, str]:
    """The throwaway crossband's environment.

    ``CROSSBAND_MEMORY_URL`` matters most. Crossband's default is the real
    membro on port 8901, so the launcher forces it here and then checks
    ``/api/state`` reports the throwaway address. ``MEMORY_AUTH_TOKEN`` is
    the throwaway membro's token, so the seats' history search works as it
    does on a real install.
    """
    forced = {
        "CROSSBAND_HOST": "127.0.0.1",
        "CROSSBAND_PORT": str(port),
        "CROSSBAND_DATA_DIR": data_dir,
        "CROSSBAND_MEMORY_URL": memory_url,
        "MEMORY_AUTH_TOKEN": memory_token,
        "CROSSBAND_SIBLING_APPS": "{}",
        # No Tailscale Funnel probe and no voice matcher: neither is part of
        # answering a typed question.
        "CROSSBAND_FUNNEL_CHECK_S": "0",
        "CROSSBAND_VOICE_ID_ENABLED": "false",
        "CROSSBAND_REQUIRE_KEYS": "false",
        "ANTHROPIC_BASE_URL": f"{meter_prefix}.crossband/anthropic",
        "OPENAI_BASE_URL": f"{meter_prefix}.crossband/openai/v1",
    }
    if anthropic_model:
        # A fresh data directory seeds the Claude seat from this value.
        forced["CROSSBAND_ANTHROPIC_MODEL"] = anthropic_model
    return build_child_env(forced=forced, home=home, credentials=credentials,
                           source_env=source_env)
