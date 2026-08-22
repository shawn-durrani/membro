"""The launchd supervisor is a placeholder template rendered by
ops/install-supervisor.sh at install time. These tests pin the things that make
it safe and correct without needing macOS or launchd:

  1. The committed template carries NO real path — only placeholders — so no
     personal home directory can leak into the repo (the same discipline
     scripts/secret-scan.sh enforces).
  2. After substitution the result is a valid plist that actually configures a
     self-restarting, run-at-load agent pointing at start.sh.
  3. It supervises MEMBRO, on Membro's port — a copy-paste of a sibling service's agent
     with its label or port left behind would fight that sibling for its port and
     leave memory unsupervised, which is the exact failure the supervisor exists to end.

Keyless and cross-platform: plistlib is stdlib, so this runs in CI on Linux
where `plutil` does not exist.
"""
import plistlib
import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "ops" / "dev.membro.server.plist.template"
INSTALLER = REPO / "ops" / "install-supervisor.sh"

PLACEHOLDERS = ("{{REPO_DIR}}", "{{HOME}}", "{{PATH}}")


def _render(repo_dir="/opt/example/membro", home="/Users/you",
            path="/usr/bin:/bin"):
    text = TEMPLATE.read_text()
    return (text.replace("{{REPO_DIR}}", repo_dir)
                .replace("{{HOME}}", home)
                .replace("{{PATH}}", path))


def test_template_and_installer_present():
    assert TEMPLATE.exists(), "the plist template ships in ops/"
    assert INSTALLER.exists(), "the installer ships in ops/"


def test_installer_is_executable():
    assert INSTALLER.stat().st_mode & stat.S_IXUSR, "installer must be runnable"


def test_template_has_no_real_path_only_placeholders():
    """No committed personal path: every machine-specific value is a
    placeholder the installer fills in. This is what keeps the identifier
    scanner green and the template reusable on any machine."""
    text = TEMPLATE.read_text()
    assert "/Users/" not in text, "a real macOS home path leaked into the template"
    for ph in PLACEHOLDERS:
        assert ph in text, f"{ph} must stay a placeholder"


def test_rendered_plist_is_valid_and_fully_substituted():
    rendered = _render()
    assert "{{" not in rendered, "an unsubstituted placeholder survived"
    plistlib.loads(rendered.encode())  # raises if malformed


def test_agent_restarts_itself_and_starts_at_login():
    """The whole point of the supervisor: a crash or a stray kill must not leave memory
    dark. KeepAlive restarts it; RunAtLoad brings it back after a reboot;
    ThrottleInterval stops a hard boot failure spinning."""
    p = plistlib.loads(_render().encode())
    assert p["KeepAlive"] is True
    assert p["RunAtLoad"] is True
    assert p["ThrottleInterval"] >= 10


def test_agent_supervises_membro_not_sideband():
    """A copy-paste of the sibling repo's agent that kept its label or port
    would fight the sibling for its port AND leave memory unsupervised — the exact
    outage this issue exists to end, reintroduced silently."""
    p = plistlib.loads(_render().encode())
    assert p["Label"] == "dev.membro.server"
    assert "sideband" not in TEMPLATE.read_text().lower().replace(
        "sideband keeps answering", "").replace("sideband's", "")
    installer = INSTALLER.read_text()
    assert 'PORT="${MEMORY_PORT:-8901}"' in installer, \
        "the installer must stop a hand-started MEMBRO, on Membro's port"
    assert "8902" not in installer


def test_agent_runs_start_sh_from_the_repo_it_was_installed_from():
    p = plistlib.loads(_render(repo_dir="/opt/example/membro").encode())
    assert p["ProgramArguments"][-1] == "/opt/example/membro/start.sh"
    assert p["WorkingDirectory"] == "/opt/example/membro"


def test_agent_gets_a_usable_path_and_home():
    """launchd hands an agent almost no environment; start.sh needs python3 to
    build or refresh the venv, and HOME for the venv/pip caches."""
    p = plistlib.loads(_render(home="/Users/you", path="/usr/bin:/bin").encode())
    env = p["EnvironmentVariables"]
    assert env["HOME"] == "/Users/you"
    assert "/usr/bin" in env["PATH"]


def test_installer_takes_over_as_sole_owner():
    """Two owners of one port is the race the supervisor exists to end: it must
    drop any prior agent AND stop a hand-started instance before bootstrapping."""
    text = INSTALLER.read_text()
    assert "launchctl bootout" in text
    assert "lsof" in text and "kill" in text
    assert "launchctl bootstrap" in text
    assert "plutil -lint" in text, "a malformed plist must never be loaded"
    assert "{{" in text and "unsubstituted" in text, \
        "the installer must refuse a template it failed to substitute"
