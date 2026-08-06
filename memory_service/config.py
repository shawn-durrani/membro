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
    # flow as the gate — in practice one Tailscale tailnet name (#83). Empty
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
    # Model for image captions (#107). Empty = use miner_model. Set this only
    # if your miner is a text-only model: captions need a vision-capable one.
    caption_model: str = ""
    # The summary is the most-read artifact in the system (every model, every
    # round) and rebuilds are infrequent — a stronger model is worth it here
    # while the cheap miner keeps the high-volume extraction work.
    summary_model: str = "claude-sonnet-5"
    embedding_model: str = "text-embedding-3-small"
    memory_summary_words: int = 2000
    # (`summary_emergent_topics` lived here until 2026-07-26 (#13). Emergent
    # middle sections graduated from experiment to simply how the profile is
    # written, so the knob is gone; an old value left in config.local.json is
    # ignored, like any unknown key. See summary.py for how to revert.)

    # trust & walls
    trusted_apps: list[str] = []  # register your own client app slugs; empty = no app-trusted writes
    grounding_allowlist: list[str] = []    # user-specific ubiquitous nouns, config not code

    contract_version: str = "1.1"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "memory.db"


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
    if os.environ.get("MEMORY_DATA_DIR"):
        merged["data_dir"] = os.environ["MEMORY_DATA_DIR"]
    if os.environ.get("MEMORY_MIRROR_DIR"):
        merged["mirror_dir"] = os.environ["MEMORY_MIRROR_DIR"]
    if os.environ.get("MEMORY_BACKUP_INTERVAL_HOURS"):
        merged["backup_interval_hours"] = os.environ["MEMORY_BACKUP_INTERVAL_HOURS"]
    return Settings(**merged)
