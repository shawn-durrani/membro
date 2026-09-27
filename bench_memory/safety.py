"""The proof that a benchmark target is a throwaway membro.

A benchmark writes thousands of synthetic facts, so it must never reach your
real ledger. A port number proves nothing about who is listening, so the
harness binds three things together before it writes anything:

* the operator's acknowledgement, ``DISPOSABLE_ACK_ENV=1``,
* a data directory with no real-store markers that carries a sentinel file
  holding a fresh random token, and
* the running membro, which must echo that same token back from
  ``GET /v1/disposable-identity``.

Membro reads the sentinel from the data directory it serves, so an endpoint
that returns the token found on disk is serving that throwaway directory. The
real instance has no sentinel and answers ``disposable: false``. An unrelated
server can't know the token.

``require_disposable_target`` is the local half and ``verify_endpoint_is_
disposable`` the endpoint half. The endpoint half is a point-in-time proof, so
the launcher repeats it before every write and every deletion.

The sentinel read here and the one in ``memory_service/api.py`` are one
contract: same file name, same 4096-byte cap, strict UTF-8, never a symlink,
never a non-regular file. ``tests/test_bench_memory_safety.py`` holds the two
to the same verdict on every hostile input. The harness never imports the
service, so the name and the rules are written out in both places.
"""

from __future__ import annotations

import hmac
import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Necessary, never sufficient: the sentinel and the token round trip still
# have to pass.
DISPOSABLE_ACK_ENV = "BENCH_MEMORY_I_UNDERSTAND_THIS_IS_DISPOSABLE"

# Must equal memory_service.api.DISPOSABLE_SENTINEL_NAME.
DISPOSABLE_MARKER_NAME = ".bench_memory_disposable"
DISPOSABLE_IDENTITY_PATH = "/v1/disposable-identity"
DISPOSABLE_MARKER_MAX_BYTES = 4096

# What a real membro data directory carries.
_PROD_PATH_MARKERS = ("memory.db", "backups")


class UnsafeLiveTarget(RuntimeError):
    """The target couldn't be proven to be a throwaway membro."""


def read_marker_token(marker_path: Path) -> str | None:
    """The sentinel's token, or ``None`` when it isn't a valid sentinel.

    Never follows a symlink, requires a regular file, opens non-blocking so a
    FIFO can't hang the read, rejects a file over the cap and rejects bytes
    that aren't UTF-8. Any filesystem problem is ``None``, never an exception.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    try:
        if not nofollow and marker_path.is_symlink():
            return None
        fd = os.open(marker_path, os.O_RDONLY | nofollow | nonblock)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, DISPOSABLE_MARKER_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > DISPOSABLE_MARKER_MAX_BYTES:
        return None
    try:
        return raw.decode("utf-8").strip() or None
    except UnicodeDecodeError:
        return None


@dataclass(frozen=True)
class DisposableTarget:
    """A data directory proven throwaway on disk. Holding one is not proof
    that the endpoint serves it: that's ``verify_endpoint_is_disposable``."""
    data_dir: Path
    base_url: str
    token: str


def require_disposable_target(data_dir: Path | str, *, base_url: str | None,
                              env: dict | None = None,
                              allow_existing_store: bool = False
                              ) -> DisposableTarget:
    """The local half of the proof. Refuses unless every condition holds.

    ``allow_existing_store`` admits a directory that already holds a
    ``memory.db``, which a store built earlier by the harness does. The
    sentinel is read first, so a real data directory, which has none, is
    still refused.
    """
    env = os.environ if env is None else env
    if env.get(DISPOSABLE_ACK_ENV) != "1":
        raise UnsafeLiveTarget(
            f"set {DISPOSABLE_ACK_ENV}=1 to confirm the benchmark target is "
            "a throwaway you're happy to delete")
    parts = urllib.parse.urlsplit(base_url or "")
    if not parts.scheme or not parts.hostname:
        raise UnsafeLiveTarget(f"{base_url!r} isn't a usable http endpoint")

    path = Path(data_dir)
    marker = path / DISPOSABLE_MARKER_NAME
    presumed = read_marker_token(marker)
    if not (allow_existing_store and presumed):
        for name in _PROD_PATH_MARKERS:
            if (path / name).exists():
                raise UnsafeLiveTarget(
                    f"{path} contains {name!r}, which is what a real membro "
                    "data directory looks like")
    token = read_marker_token(marker)
    if not token:
        raise UnsafeLiveTarget(
            f"{path} carries no valid {DISPOSABLE_MARKER_NAME} sentinel, so "
            "nothing ties it to a throwaway membro")
    return DisposableTarget(data_dir=path, base_url=base_url, token=token)


def _fetch_identity(base_url: str, timeout: float):
    url = base_url.rstrip("/") + DISPOSABLE_IDENTITY_PATH
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.hostname not in _LOOPBACK_HOSTS:
        raise UnsafeLiveTarget(f"refusing to probe {url}: loopback http only")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise UnsafeLiveTarget(
            f"{url} couldn't be reached ({exc}), so it can't prove it's "
            "throwaway") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeLiveTarget(f"{url} didn't return JSON ({exc})") from exc


def verify_endpoint_is_disposable(target: DisposableTarget, *, fetch=None,
                                  timeout: float = 5.0) -> None:
    """The endpoint half: the running membro must echo the sentinel's token.

    ``fetch`` is the test hook, a callable ``(base_url, timeout)`` returning
    the decoded reply.
    """
    payload = (fetch or _fetch_identity)(target.base_url, timeout)
    if not isinstance(payload, dict):
        raise UnsafeLiveTarget(f"{target.base_url} sent a malformed identity reply")
    if payload.get("disposable") is not True:
        raise UnsafeLiveTarget(
            f"{target.base_url} reports disposable="
            f"{payload.get('disposable')!r}, which is what a real membro "
            f"answers. It isn't serving {target.data_dir}.")
    echoed = payload.get("token")
    if not isinstance(echoed, str) or not echoed:
        raise UnsafeLiveTarget(f"{target.base_url} claims disposable but sent no token")
    if not hmac.compare_digest(echoed, target.token):
        raise UnsafeLiveTarget(
            f"{target.base_url} echoed a different token from the sentinel in "
            f"{target.data_dir}. Another instance is answering on that port.")
