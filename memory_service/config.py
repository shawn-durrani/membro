"""Configuration — one validated module, no scattered defaults.

Precedence: built-in defaults < config.json < config.local.json < environment.
Fails loudly when a capability is invoked without its key (no silent no-ops).
"""

import json
import os
from pathlib import Path

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    # server
    host: str = "127.0.0.1"
    port: int = 8901
    auth_token: str | None = None          # required to bind non-loopback
    # Hostnames where the BROWSER surface is admitted with the password/session
    # flow as the gate — in practice one Tailscale tailnet name. Empty
    # (the default) keeps the strict loopback-or-bearer-token rule exactly as
    # it was: this can only ever widen access for an operator who opts in by
    # naming their own host. See api.py's trust middleware and SECURITY.md.
    trusted_hosts: list[str] = []

    # storage
    data_dir: Path = REPO_ROOT / "data"
    backup_keep: int = 14
    backup_interval_hours: float = 6.0
    mirror_dir: Path | None = None         # optional second-folder snapshot mirror
    mirror_keep: int = 7

    # identity & models
    user_name: str = "User"
    miner_model: str = "claude-haiku-4-5"  # any lab's cheap model; routed by name
    # Model for image captions. Empty = use miner_model. Set this only
    # if your miner is a text-only model: captions need a vision-capable one.
    caption_model: str = ""
    # The summary is the most-read artifact in the system (every model, every
    # round) and rebuilds are infrequent — a stronger model is worth it here
    # while the cheap miner keeps the high-volume extraction work.
    summary_model: str = "claude-sonnet-5"
    # OpenAI-compatible endpoint for every non-claude model name (#59):
    # Ollama, MLX, LM Studio, or OpenAI itself. Empty = the SDK default
    # (api.openai.com). With a base URL set, no API key is required.
    # Point it at an always-on server: mining depends on it, and a server
    # tied to another app's lifecycle makes mining depend on that app.
    llm_base_url: str = ""
    # The judge pass (#58): a flag-gated background sweep that clears a
    # grounding hold only on a verified witness quote, and relabels the
    # persona hold into its own bulk-review group. Off by default; every
    # judge failure leaves a row exactly as it was.
    judge_pass: bool = False
    judge_model: str = ""  # empty = miner_model; routed by name like any other
    embedding_model: str = "text-embedding-3-small"
    # OpenAI-compatible endpoint for embeddings (#60), independent of
    # `llm_base_url` because chat serving and embedding serving are often
    # different servers. Empty = the SDK default. Changing embedding_model
    # drops every stored vector and re-embeds in the background; recall is
    # keyword-only until that completes.
    embedding_base_url: str = ""
    memory_summary_words: int = 2000
    # (`summary_emergent_topics` lived here until 2026-07-26. Emergent
    # middle sections graduated from experiment to simply how the profile is
    # written, so the knob is gone; an old value left in config.local.json is
    # ignored, like any unknown key. See summary.py for how to revert.)

    # trust & walls
    trusted_apps: list[str] = []  # register your own client app slugs; empty = no app-trusted writes
    grounding_allowlist: list[str] = []    # user-specific ubiquitous nouns, config not code

    # 1.5 (#93): guest_speakers on POST /facts, held as guest-present.
    # 1.4 (#84): web_sources on /search hits, browser_origin on /health, the
    # per-conversation watermark route, event_date as a calendar day.
    # 1.3 (#55): web_sources on ingest + facts. 1.2 (#33): speaker_identity.
    contract_version: str = "1.5"
    # The origin a browser on a phone can reach this service at, reported on
    # /v1/health as `browser_origin` (contract 1.4) so a client app links the
    # admin surface at an address that works instead of guessing a port.
    # Empty (the default) derives it: https on `tailscale_port` at the first
    # trusted host when the browser surface is admitted from a tailnet name,
    # otherwise loopback on `port`.
    browser_origin: str = ""
    tailscale_port: int = 8443  # scripts/tailscale-serve.sh's default

    @property
    def db_path(self) -> Path:
        return self.data_dir / "memory.db"

    def reachable_browser_origin(self) -> str:
        """Where a browser can open this service: the operator's explicit
        value, else the tailnet address when one is trusted, else loopback."""
        if self.browser_origin.strip():
            return self.browser_origin.strip().rstrip("/")
        if self.trusted_hosts:
            return f"https://{self.trusted_hosts[0]}:{self.tailscale_port}"
        return f"http://127.0.0.1:{self.port}"


def load_settings() -> Settings:
    merged: dict = {}
    for name in ("config.json", "config.local.json"):
        p = REPO_ROOT / name
        if p.exists():
            merged.update(json.loads(p.read_text()))
    if os.environ.get("MEMORY_PORT"):
        merged["port"] = int(os.environ["MEMORY_PORT"])
    if os.environ.get("MEMORY_AUTH_TOKEN"):
        merged["auth_token"] = os.environ["MEMORY_AUTH_TOKEN"]
    if os.environ.get("MEMORY_TRUSTED_HOSTS"):
        merged["trusted_hosts"] = [h.strip().lower()
                                   for h in os.environ["MEMORY_TRUSTED_HOSTS"].split(",")
                                   if h.strip()]
    if os.environ.get("MEMORY_BROWSER_ORIGIN"):
        merged["browser_origin"] = os.environ["MEMORY_BROWSER_ORIGIN"]
    if os.environ.get("MEMORY_TAILSCALE_PORT"):
        merged["tailscale_port"] = int(os.environ["MEMORY_TAILSCALE_PORT"])
    if os.environ.get("MEMORY_DATA_DIR"):
        merged["data_dir"] = os.environ["MEMORY_DATA_DIR"]
    if os.environ.get("MEMORY_MIRROR_DIR"):
        merged["mirror_dir"] = os.environ["MEMORY_MIRROR_DIR"]
    if os.environ.get("MEMORY_BACKUP_INTERVAL_HOURS"):
        merged["backup_interval_hours"] = os.environ["MEMORY_BACKUP_INTERVAL_HOURS"]
    return Settings(**merged)
