"""Start, prove and stop the throwaway membro and crossband.

A run has one workspace: a folder outside both checkouts, marked with a
sentinel token, holding one worktree of each app. Each question then gets a
fresh pair of services with their own data folders, ports and logs inside
that workspace, and the pair is stopped and deleted when the question is
done. Nothing here reads your ``.env``, runs ``start.sh``, runs from a live
checkout, or writes to a data folder it didn't create.

Ports are only for liveness. Losing a race for a free port can only fail a
start, which is retried. Identity is the sentinel token, proven before the
first write and again before every write after it.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..safety import (
    DISPOSABLE_MARKER_NAME,
    DisposableTarget,
    UnsafeLiveTarget,
    read_marker_token,
    require_disposable_target,
    verify_endpoint_is_disposable,
)
from . import worktree as wt
from .env_allowlist import crossband_env, membro_env

# The fleet's own ports, and the ones either side of them. Refusing them is
# a sanity guard. The token is what makes a launch safe.
RESERVED_PORTS = frozenset(range(8890, 8911))

# What a real membro or crossband data folder holds.
_REAL_DATA_MARKERS = ("memory.db", "chat.db", "backups")

# The throwaway membro's config. Crossband writes under this app name, and
# membro holds writes from an app it doesn't trust for review, which would
# keep every mined fact out of recall and the profile.
SOURCE_APP = "multi-model-chat"  # secret-scan: allow (crossband's wire value)
MEMBRO_CONFIG = {"trusted_apps": [SOURCE_APP]}


class LaunchError(RuntimeError):
    """A throwaway service couldn't be started or proven."""


def pick_free_port(*, exclude=RESERVED_PORTS, attempts: int = 50) -> int:
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in exclude:
            return port
    raise LaunchError("no free loopback port outside the reserved range")


def write_sentinel(data_dir: Path) -> str:
    """Mark a new data folder as throwaway, before its service first starts."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    (data_dir / DISPOSABLE_MARKER_NAME).write_text(token, encoding="utf-8")
    return token


def assert_dir_is_not_real(path: Path) -> None:
    present = [m for m in _REAL_DATA_MARKERS if (Path(path) / m).exists()]
    if present:
        raise LaunchError(f"{path} contains {present}, which is what a real "
                          "data folder looks like")


# -------------------------------------------------------------------- services

@dataclass
class ServiceHandle:
    name: str
    proc: subprocess.Popen
    port: int
    base_url: str
    data_dir: Path
    log_path: Path
    log_file: object | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid

    def is_alive(self) -> bool:
        return self.proc.poll() is None


def _spawn(*, name: str, python: Path, module: str, cwd: Path, env: dict,
           port: int, data_dir: Path, log_path: Path) -> ServiceHandle:
    log = open(log_path, "wb")
    proc = subprocess.Popen(
        [str(python), "-m", module], cwd=str(cwd), env=env,
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        # Its own process group, so stop() signals this tree and nothing else.
        start_new_session=True)
    return ServiceHandle(name=name, proc=proc, port=port,
                         base_url=f"http://127.0.0.1:{port}",
                         data_dir=Path(data_dir), log_path=Path(log_path),
                         log_file=log)


def http_json(url: str, *, payload=None, method=None, timeout: float = 30.0,
              token: str | None = None, expect_json: bool = True):
    """A loopback-only request. ``expect_json=False`` drains a stream to its
    end and returns it as text."""
    if urllib.parse.urlsplit(url).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise LaunchError(f"refusing non-loopback request to {url}")
    data, headers = None, {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    if not expect_json:
        return body.decode("utf-8", errors="replace")
    return json.loads(body.decode("utf-8")) if body else None


def log_tail(handle: ServiceHandle, lines: int = 25) -> str:
    try:
        return "\n".join(handle.log_path.read_text(errors="replace")
                         .splitlines()[-lines:])
    except OSError:
        return "(no log)"


def wait_healthy(handle: ServiceHandle, path: str, *, timeout: float = 60.0):
    url = handle.base_url + path
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if not handle.is_alive():
            raise LaunchError(
                f"{handle.name} exited with code {handle.proc.returncode} while "
                f"starting.\n--- {handle.log_path} (tail) ---\n{log_tail(handle)}")
        try:
            return http_json(url, timeout=3.0)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last = str(exc)
            time.sleep(0.25)
    raise LaunchError(f"{handle.name} didn't answer {url} within {timeout}s "
                      f"({last}).\n--- {handle.log_path} (tail) ---\n"
                      f"{log_tail(handle)}")


def owns_port(handle: ServiceHandle) -> bool | None:
    """Whether our own child holds its port. ``None`` means lsof isn't there
    to ask, which doesn't block a launch the token has already proven."""
    if not shutil.which("lsof"):
        return None
    proc = subprocess.run(["lsof", "-nP", f"-iTCP:{handle.port}",
                           "-sTCP:LISTEN", "-t"], capture_output=True, text=True)
    pids = {int(p) for p in proc.stdout.split() if p.strip().isdigit()}
    if not pids:
        return False
    return handle.pid in pids or any(_has_ancestor(p, handle.pid) for p in pids)


def _has_ancestor(pid: int, ancestor: int, *, max_depth: int = 6) -> bool:
    for _ in range(max_depth):
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
        if not out.isdigit():
            return False
        pid = int(out)
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
    return False


def stop(handle: ServiceHandle, *, grace: float = 20.0) -> None:
    if handle.proc.poll() is None:
        try:
            pgid = os.getpgid(handle.pid)
        except OSError:
            pgid = None
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM) if pgid else handle.proc.terminate()
        try:
            handle.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL) if pgid else handle.proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                handle.proc.wait(timeout=5.0)
    if handle.log_file is not None:
        with contextlib.suppress(OSError):
            handle.log_file.close()
        handle.log_file = None


def remove_disposable_tree(root: Path, token: str) -> None:
    """Delete a throwaway folder, only after re-reading its sentinel.

    A folder that no longer proves itself is left where it is. An orphaned
    temp folder is a better outcome than deleting the wrong tree.
    """
    root = Path(root)
    if not root.exists():
        return
    found = read_marker_token(root / DISPOSABLE_MARKER_NAME)
    if not found or not hmac.compare_digest(found, token):
        raise UnsafeLiveTarget(
            f"not deleting {root}: its {DISPOSABLE_MARKER_NAME} doesn't hold "
            "this run's token. Check it and remove it by hand.")
    shutil.rmtree(root)


# ------------------------------------------------------------------ workspace

@dataclass
class Workspace:
    """The run's throwaway folder and the two worktrees in it."""
    root: Path
    token: str
    membro_repo: Path
    crossband_repo: Path
    membro_tree: Path
    crossband_tree: Path | None
    commits: dict = field(default_factory=dict)


def open_workspace(*, root: Path, membro_repo: Path, crossband_repo: Path,
                   membro_ref: str, crossband_ref: str,
                   with_crossband: bool = True) -> Workspace:
    root, membro_repo, crossband_repo = (Path(root), Path(membro_repo),
                                         Path(crossband_repo))
    wt.assert_outside_live_checkouts(root, membro_repo, crossband_repo)
    if root.exists() and any(root.iterdir()):
        raise LaunchError(f"{root} isn't empty; give each run a fresh root")
    root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(16)
    (root / DISPOSABLE_MARKER_NAME).write_text(token, encoding="utf-8")
    trees = []
    try:
        membro_tree, membro_commit = wt.add(membro_repo, root / "membro-tree",
                                            membro_ref)
        trees.append((membro_repo, membro_tree))
        wt.write_config(membro_tree, MEMBRO_CONFIG)
        commits = {"membro": membro_commit}
        crossband_tree = None
        if with_crossband:
            crossband_tree, crossband_commit = wt.add(
                crossband_repo, root / "crossband-tree", crossband_ref)
            trees.append((crossband_repo, crossband_tree))
            commits["crossband"] = crossband_commit
    except BaseException:
        for repo, tree in reversed(trees):
            wt.remove(repo, tree)
        with contextlib.suppress(Exception):
            remove_disposable_tree(root, token)
        raise
    return Workspace(root=root, token=token, membro_repo=membro_repo,
                     crossband_repo=crossband_repo, membro_tree=membro_tree,
                     crossband_tree=crossband_tree, commits=commits)


def close_workspace(ws: Workspace, *, remove: bool = True) -> None:
    wt.remove(ws.membro_repo, ws.membro_tree)
    if ws.crossband_tree is not None:
        wt.remove(ws.crossband_repo, ws.crossband_tree)
    if remove:
        remove_disposable_tree(ws.root, ws.token)


# ----------------------------------------------------------------------- pair

@dataclass
class Pair:
    """A running, proven throwaway membro, and crossband when asked for."""
    folder: Path
    membro: ServiceHandle
    crossband: ServiceHandle | None
    target: DisposableTarget
    membro_token: str

    def reverify(self) -> None:
        """Prove the pair again before a write: the token still round-trips,
        both children are alive, and each still holds its own port."""
        verify_endpoint_is_disposable(self.target)
        for handle in (h for h in (self.membro, self.crossband) if h):
            if not handle.is_alive():
                raise UnsafeLiveTarget(
                    f"{handle.name} stopped (exit {handle.proc.returncode})")
            if owns_port(handle) is False:
                raise UnsafeLiveTarget(
                    f"{handle.name}: port {handle.port} is held by a process "
                    "the harness didn't start")


def start_pair(ws: Workspace, name: str, *, meter_prefix: str, credentials: dict,
               anthropic_model: str | None = None, seed_store: Path | None = None,
               with_crossband: bool = True, env: dict | None = None,
               boot_timeout: float = 90.0) -> Pair:
    """A fresh pair in ``<workspace>/<name>``.

    With ``seed_store`` the membro starts from a copy of a store built
    earlier. That copy carries a ``memory.db`` by definition, so the real
    store check can't apply to it, and the fresh sentinel written into the
    copy is the proof.
    """
    env = os.environ if env is None else env
    folder = ws.root / name
    home, logs = folder / "home", folder / "logs"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    membro_data, crossband_data = folder / "membro-data", folder / "crossband-data"
    if seed_store:
        shutil.copytree(seed_store, membro_data, dirs_exist_ok=False)
    else:
        assert_dir_is_not_real(membro_data)
    write_sentinel(membro_data)
    crossband_data.mkdir(parents=True, exist_ok=True)
    assert_dir_is_not_real(crossband_data)

    membro_token = secrets.token_urlsafe(32)
    started: list[ServiceHandle] = []
    try:
        port = pick_free_port()
        target = require_disposable_target(
            membro_data, base_url=f"http://127.0.0.1:{port}", env=env,
            allow_existing_store=bool(seed_store))
        membro = _spawn(
            name="membro", python=ws.membro_repo / ".venv" / "bin" / "python",
            module="memory_service.api", cwd=ws.membro_tree,
            env=membro_env(data_dir=str(membro_data), port=port, home=str(home),
                           auth_token=membro_token, meter_prefix=meter_prefix,
                           credentials=credentials, source_env=env),
            port=port, data_dir=membro_data, log_path=logs / "membro.log")
        started.append(membro)
        wait_healthy(membro, "/v1/health", timeout=boot_timeout)
        # The endpoint half of the proof, before anything writes.
        verify_endpoint_is_disposable(target)

        if not with_crossband:
            return Pair(folder=folder, membro=membro, crossband=None,
                        target=target, membro_token=membro_token)
        if ws.crossband_tree is None:
            raise LaunchError("this workspace has no crossband worktree")
        cport = pick_free_port()
        crossband = _spawn(
            name="crossband",
            python=ws.crossband_repo / ".venv" / "bin" / "python",
            module="backend", cwd=ws.crossband_tree,
            env=crossband_env(data_dir=str(crossband_data), port=cport,
                              memory_url=membro.base_url, home=str(home),
                              memory_token=membro_token, meter_prefix=meter_prefix,
                              credentials=credentials,
                              anthropic_model=anthropic_model, source_env=env),
            port=cport, data_dir=crossband_data,
            log_path=logs / "crossband.log")
        started.append(crossband)
        state = wait_healthy(crossband, "/api/state", timeout=boot_timeout)
        memory = state.get("memory") or {}
        # Crossband's default memory address is the real membro, so check the
        # wiring took instead of assuming it.
        if memory.get("url") != membro.base_url:
            raise UnsafeLiveTarget(
                f"crossband reports memory url {memory.get('url')!r}, not the "
                f"throwaway membro at {membro.base_url!r}")
        if not memory.get("available"):
            raise LaunchError("crossband can't reach the throwaway membro, so "
                              "the run would measure a seat with no memory")
    except BaseException:
        for handle in reversed(started):
            stop(handle)
        raise
    return Pair(folder=folder, membro=membro, crossband=crossband,
                target=target, membro_token=membro_token)


def stop_pair(pair: Pair, ws: Workspace, *, remove: bool = True) -> None:
    """Stop both services, then delete the pair's folder once the workspace
    sentinel still proves it's ours."""
    if pair.crossband is not None:
        stop(pair.crossband)
    stop(pair.membro)
    if remove:
        found = read_marker_token(ws.root / DISPOSABLE_MARKER_NAME)
        if not found or not hmac.compare_digest(found, ws.token):
            raise UnsafeLiveTarget(f"not deleting {pair.folder}: the workspace "
                                   "sentinel no longer matches this run")
        shutil.rmtree(pair.folder, ignore_errors=True)
