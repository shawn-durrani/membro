"""Throwaway git worktrees: where each service under test runs from.

Both apps pin their root at import (``ROOT = Path(__file__)...``) and read
``.env`` and ``config.local.json`` from it. Run from your live checkout, a
throwaway crossband would load your real keys and settings, and membro would
read your real config. A detached worktree moves that root. It's the
mechanism that keeps real config out, so a direct run from the live checkout
is never a substitute.

``.env``, ``config.local.json``, ``data/`` and ``.venv`` are gitignored in both
repos, so a fresh worktree has none of them. ``assert_clean`` checks anyway.

A worktree has no virtualenv. Neither app is pip-installed, so ``python -m``
finds the app's code from the working directory, and the launcher runs the
live checkout's interpreter with the worktree as its working directory. Code
and root come from the worktree, libraries from the venv.
``assert_requirements_match`` refuses that pairing when the two trees want
different libraries.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

_FORBIDDEN_IN_WORKTREE = (".env", "config.local.json")


class WorktreeError(RuntimeError):
    """A throwaway worktree couldn't be set up safely."""


def _git(repo_root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo_root), *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed in {repo_root}: "
                            f"{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def assert_outside_live_checkouts(dest: Path, *live_roots: Path) -> None:
    """Refuse a throwaway path inside a real checkout, so teardown's
    recursive delete can never reach real work."""
    dest = Path(dest).resolve()
    for root in live_roots:
        root = Path(root).resolve()
        if dest == root or root in dest.parents:
            raise WorktreeError(f"{dest} is inside the checkout {root}; "
                                "throwaway trees must live outside both repos")


def assert_clean(worktree: Path) -> None:
    present = [n for n in _FORBIDDEN_IN_WORKTREE if (Path(worktree) / n).exists()]
    if present:
        raise WorktreeError(f"{worktree} contains {present}; a throwaway tree "
                            "must carry no real configuration")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_requirements_match(live_root: Path, worktree: Path) -> None:
    live_req = Path(live_root) / "requirements.txt"
    tree_req = Path(worktree) / "requirements.txt"
    if not live_req.exists() or not tree_req.exists():
        raise WorktreeError("requirements.txt missing, so the venv can't be "
                            "matched to the code")
    if _digest(live_req) != _digest(tree_req):
        raise WorktreeError(
            f"{live_root}/.venv was built for a different requirements.txt "
            f"from the one at {worktree}. Update the venv, or pick a ref "
            "with the same requirements.")


def add(repo_root: Path, dest: Path, ref: str) -> tuple[Path, str]:
    """A detached worktree of ``ref`` at ``dest``, and the commit it holds."""
    repo_root, dest = Path(repo_root), Path(dest)
    if dest.exists():
        raise WorktreeError(f"refusing to reuse existing path {dest}")
    _git(repo_root, "worktree", "add", "--detach", str(dest), ref)
    try:
        assert_clean(dest)
        assert_requirements_match(repo_root, dest)
    except WorktreeError:
        remove(repo_root, dest)
        raise
    return dest, _git(dest, "rev-parse", "HEAD")


def write_config(worktree: Path, config: dict) -> None:
    """Give a clean worktree its throwaway ``config.local.json``."""
    assert_clean(worktree)
    (Path(worktree) / "config.local.json").write_text(json.dumps(config, indent=1))


def remove(repo_root: Path, dest: Path) -> None:
    """Remove a worktree and prune its record. Never raises."""
    for args in (("worktree", "remove", "--force", str(dest)),
                 ("worktree", "prune")):
        try:
            _git(Path(repo_root), *args)
        except WorktreeError:
            pass
