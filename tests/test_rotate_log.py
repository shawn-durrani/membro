"""The service log rolls over at start (#202).

launchd appends everything the service prints to data/service.log and never
trims it. start.sh runs ops/rotate-log.sh first: past 10MB the log is copied
to service.log.1 and emptied in place, so launchd's open handle stays valid.
Both files stay owner-only, like everything else in data/ (#133).
"""

import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROTATE = REPO / "ops" / "rotate-log.sh"
START = REPO / "start.sh"
TEMPLATE = REPO / "ops" / "dev.membro.server.plist.template"


def _rotate(log: Path, max_bytes: int | None = None) -> str:
    args = ["bash", str(ROTATE), str(log)]
    if max_bytes is not None:
        args.append(str(max_bytes))
    return subprocess.run(args, check=True, capture_output=True,
                          text=True).stdout


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_the_log_rolls_over_past_its_cap(tmp_path):
    log = tmp_path / "service.log"
    log.write_text("x" * 200)
    os.chmod(log, 0o600)
    (tmp_path / "service.log.1").write_text("the generation before")
    inode = log.stat().st_ino
    assert "rotated" in _rotate(log, 100)
    assert log.read_text() == "" and log.stat().st_ino == inode
    old = tmp_path / "service.log.1"
    assert old.read_text() == "x" * 200
    assert _mode(old) == 0o600 and _mode(log) == 0o600


def test_the_copy_is_owner_only_even_from_a_looser_log(tmp_path):
    log = tmp_path / "service.log"
    log.write_text("x" * 200)
    os.chmod(log, 0o644)
    old = tmp_path / "service.log.1"
    old.write_text("older")
    os.chmod(old, 0o644)
    _rotate(log, 100)
    assert _mode(old) == 0o600


def test_a_log_under_its_cap_is_left_alone(tmp_path):
    log = tmp_path / "service.log"
    log.write_text("x" * 50)
    assert _rotate(log, 100) == ""
    assert log.read_text() == "x" * 50
    assert not (tmp_path / "service.log.1").exists()
    assert _rotate(tmp_path / "missing.log", 100) == ""


def test_the_default_cap_is_ten_megabytes(tmp_path):
    log = tmp_path / "service.log"
    log.write_bytes(b"x" * (10 * 1024 * 1024))
    assert _rotate(log) == ""
    with log.open("ab") as f:
        f.write(b"x")
    assert "rotated" in _rotate(log)


def test_start_rotates_the_log_the_agent_writes():
    """start.sh rotates before anything else prints, and the file it rotates
    is the one the plist sends output to."""
    start = START.read_text()
    assert "bash ops/rotate-log.sh data/service.log" in start
    assert start.index("rotate-log.sh") < start.index("echo ")
    assert "{{REPO_DIR}}/data/service.log" in TEMPLATE.read_text()
    assert os.access(ROTATE, os.X_OK)
