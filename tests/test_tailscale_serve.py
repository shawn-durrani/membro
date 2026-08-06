"""scripts/tailscale-serve.sh — the opt-in, tailnet-only remote-access route
(#51 slice 2). Keyless and hermetic: no real Tailscale install is required or
assumed. A fake `tailscale` CLI is planted on PATH so these tests exercise the
script's own logic (locate → check signed-in → serve → verify → report),
never a real network.

What matters most here is the SAFETY property: this script must never be able
to leave a device Funnel-exposed (public internet) without refusing loudly,
and it must degrade to a clear skip — never a crash — when Tailscale isn't
installed or isn't signed in, exactly like start.sh already does for missing
API keys.
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "tailscale-serve.sh"


def _make_fake_tailscale(bin_dir: Path, *, signed_in=True, serve_ok=True,
                          funnel_on=False, dns_name="mymac.tailXXXX.ts.net",
                          log_path: Path | None = None):
    """Write a fake `tailscale` executable that answers just enough of the CLI
    surface tailscale-serve.sh actually calls."""
    fake = bin_dir / "tailscale"
    log_line = f'echo "$@" >> "{log_path}"\n' if log_path is not None else ""
    funnel_word = "Funnel on" if funnel_on else "tailnet only"
    body = f"""#!/usr/bin/env bash
{log_line}
case "$1" in
  status)
    if [ "$2" = "--json" ]; then
      echo '{{"Self": {{"DNSName": "{dns_name}."}}}}'
      exit 0
    fi
    {"exit 0" if signed_in else "exit 1"}
    ;;
  serve)
    if [ "$2" = "status" ]; then
      echo "https://{dns_name}:8443/ ({funnel_word})"
      exit 0
    fi
    {"exit 0" if serve_ok else "exit 1"}
    ;;
  *) exit 0 ;;
esac
"""
    fake.write_text(body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def run(bin_dir: Path | None, env_extra=None):
    env = dict(os.environ)
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
    else:
        # Simulate "no tailscale anywhere": a PATH with no tailscale binary on
        # it. The script must be (and is asserted elsewhere to be) PATH-only —
        # no hardcoded app-bundle fallback — so this is a true absence, not
        # dependent on what happens to be installed on the machine running
        # this test.
        env["PATH"] = "/usr/bin:/bin"
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(["bash", str(SCRIPT)], cwd=REPO, env=env,
                        capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def test_script_exists():
    assert SCRIPT.exists(), "start.sh and docs both reference this script"


# ── the CLI is never chosen implicitly: PATH, or an env var the operator set.
#    (An earlier draft fell back to the Tailscale.app bundle path, so a "no
#    tailscale on PATH" test on a machine with the real app installed silently
#    invoked the REAL binary. What makes this test file safe is that no bundle
#    path is ever USED implicitly — mentioning one in help text is fine and
#    now necessary, since on macOS that is exactly where the working CLI lives
#    (#87). Same shape as the funnel test below: comments and warnings may
#    name it; nothing may resolve to it on its own.)
def test_cli_is_never_resolved_implicitly_to_an_app_bundle():
    text = SCRIPT.read_text()
    for i, line in enumerate(text.splitlines(), 1):
        if "/Applications/" not in line:
            continue
        stripped = line.strip()
        allowed = stripped.startswith("#") or "warn" in line
        assert allowed, (
            f"line {i} uses an application-bundle path outside a "
            f"comment/warning — the CLI must come from PATH or from an "
            f"explicitly set MEMORY_TAILSCALE_BIN:\n{line}"
        )
    # the only assignments to TS_BIN are PATH lookup and the explicit env var
    assert 'TS_BIN="/Applications' not in text


# ── no Tailscale on the machine: skip cleanly, never error ───────────────────
def test_no_tailscale_cli_skips_cleanly(tmp_path):
    rc, out, err = run(bin_dir=None)
    assert rc == 0, f"absence of Tailscale must never fail the script:\n{err}"
    assert "SKIPPED" in err
    assert "127.0.0.1" in err  # reassures the reader loopback still works


# ── installed but signed out: skip cleanly, tell the user the fix ────────────
def test_signed_out_skips_cleanly(tmp_path):
    _make_fake_tailscale(tmp_path, signed_in=False)
    rc, out, err = run(tmp_path)
    assert rc == 0
    assert "SKIPPED" in err
    assert "tailscale up" in err


# ── happy path: route established, verified, URL reported ────────────────────
def test_happy_path_reports_https_url(tmp_path):
    _make_fake_tailscale(tmp_path, signed_in=True, serve_ok=True, funnel_on=False,
                         dns_name="mymac.tailXXXX.ts.net")
    rc, out, err = run(tmp_path)
    assert rc == 0, f"stderr:\n{err}"
    assert "https://mymac.tailXXXX.ts.net:8443/" in out
    # the port mount gives Membro its own ORIGIN — never a path under
    # the tailnet root, where its absolute links would hit another app
    assert "/membro" not in out


# ── the safety property: Funnel detected → refuse, never claim success ───────
def test_funnel_on_is_refused(tmp_path):
    _make_fake_tailscale(tmp_path, signed_in=True, serve_ok=True, funnel_on=True,
                         dns_name="mymac.tailXXXX.ts.net")
    rc, out, err = run(tmp_path)
    assert rc == 1, "a Funnel-exposed device must never be reported as a success"
    assert "REFUSING" in err
    assert "funnel off" in err.lower()
    # The raw `serve status` readout (showing "Funnel on") is fine to echo —
    # what must NEVER appear is this script's own success report.
    assert "Membro reachable on your tailnet" not in out


# ── a broken/older CLI: falls back to the legacy flag, still never crashes ───
def test_serve_failure_on_both_attempts_degrades_cleanly(tmp_path):
    _make_fake_tailscale(tmp_path, signed_in=True, serve_ok=False)
    rc, out, err = run(tmp_path)
    assert rc == 0, "an unrecognised CLI flag layout must degrade, not crash"
    assert "failed" in err.lower()
    assert "127.0.0.1" in err


# ── --status is read-only: never calls `serve --bg`, only reports ────────────
def test_status_flag_never_mutates(tmp_path):
    log = tmp_path / "calls.log"
    _make_fake_tailscale(tmp_path, signed_in=True, serve_ok=True, log_path=log)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    p = subprocess.run(["bash", str(SCRIPT), "--status"], cwd=REPO, env=env,
                        capture_output=True, text=True)
    assert p.returncode == 0
    calls = log.read_text()
    assert "--bg" not in calls, "--status must never establish/change the route"


# ── local port and tailnet port are configurable, with documented defaults ──
def test_port_env_vars_reach_the_serve_call(tmp_path):
    log = tmp_path / "calls.log"
    _make_fake_tailscale(tmp_path, signed_in=True, serve_ok=True, log_path=log)
    rc, out, err = run(tmp_path, env_extra={
        "MEMORY_PORT": "9999",
        "MEMORY_TAILSCALE_PORT": "9443",
    })
    assert rc == 0, err
    calls = log.read_text()
    assert "127.0.0.1:9999" in calls
    assert "--https=9443" in calls


def test_defaults_are_port_8443_onto_local_8901(tmp_path):
    log = tmp_path / "calls.log"
    _make_fake_tailscale(tmp_path, signed_in=True, serve_ok=True, log_path=log)
    rc, out, err = run(tmp_path)
    assert rc == 0, err
    calls = log.read_text()
    assert "127.0.0.1:8901" in calls
    assert "--https=8443" in calls
    # no path-mount flag layout is attempted at all any more (#82 review):
    # serving a whole origin on a port needs none, so the two-attempt
    # fallback and its version ambiguity are gone with it
    assert "--set-path" not in calls


# ── static safety check: `funnel` never appears as a command this script
#    RUNS — only inside warnings/comments/output-inspection ─────────────────
def test_script_never_invokes_funnel_as_a_command():
    text = SCRIPT.read_text()
    for i, line in enumerate(text.splitlines(), 1):
        if "funnel" not in line.lower():
            continue
        stripped = line.strip()
        allowed = (
            stripped.startswith("#")
            or "warn" in line
            or "grep" in line
            or '"funnel off"' in line
        )
        assert allowed, (
            f"line {i} mentions funnel outside a warning/check/comment — "
            f"this script must NEVER execute `tailscale funnel`:\n{line}"
        )
    assert '"$TS_BIN" funnel' not in text


# ── start.sh only calls this behind the opt-in flag, never unconditionally ───
def test_start_sh_gates_the_call_behind_the_env_flag():
    start_sh = (REPO / "start.sh").read_text()
    assert "tailscale-serve.sh" in start_sh
    idx = start_sh.index("tailscale-serve.sh")
    preceding = start_sh[:idx]
    assert "MEMORY_TAILSCALE_SERVE" in preceding.rsplit("\n\n", 1)[-1] or \
        "MEMORY_TAILSCALE_SERVE" in start_sh[:idx]
    # the call must sit inside a conditional block, not run unguarded
    line_start = start_sh.rfind("\n", 0, idx)
    call_line = start_sh[line_start:idx]
    assert "  " in call_line or "\t" in call_line, (
        "the call should be indented inside the MEMORY_TAILSCALE_SERVE guard"
    )


def test_start_sh_never_blocks_on_tailscale_failure():
    start_sh = (REPO / "start.sh").read_text()
    idx = start_sh.index("tailscale-serve.sh")
    line = start_sh[start_sh.rfind("\n", 0, idx):start_sh.find("\n", idx)]
    assert "|| true" in line, (
        "a tailscale-serve.sh failure must never stop Membro from starting"
    )


# ── an installed-but-off-PATH CLI is usable when named EXPLICITLY (#87) ──────
def test_memory_tailscale_bin_is_honoured_when_path_has_no_cli(tmp_path):
    """The macOS normal case: Tailscale is installed and working, its CLI just
    isn't on PATH. Naming it explicitly must work — while an EMPTY PATH with
    no env var still skips, so nothing is ever guessed (that guess is what
    executed a developer's real CLI during #82's test run)."""
    fake_dir = tmp_path / "elsewhere"
    fake_dir.mkdir()
    _make_fake_tailscale(fake_dir, signed_in=True, serve_ok=True,
                         dns_name="mymac.tailXXXX.ts.net")
    named = fake_dir / "tailscale"

    # PATH deliberately does NOT contain the CLI; the env var names it
    rc, out, err = run(None, env_extra={"MEMORY_TAILSCALE_BIN": str(named)})
    assert rc == 0, f"stderr:\n{err}"
    assert "https://mymac.tailXXXX.ts.net:8443/" in out


def test_no_cli_and_no_env_var_still_skips_without_guessing(tmp_path):
    rc, out, err = run(None, env_extra={"MEMORY_TAILSCALE_BIN": ""})
    assert rc == 0
    assert "SKIPPED" in err
    # the skip message points at the real fix, and no longer claims the
    # app-bundle CLI cannot serve (#87)
    assert "MEMORY_TAILSCALE_BIN" in err
    assert "does not support" not in err


def test_a_nonexecutable_env_var_target_is_ignored(tmp_path):
    dud = tmp_path / "not-executable"
    dud.write_text("#!/bin/sh\nexit 0\n")  # deliberately not chmod +x
    rc, out, err = run(None, env_extra={"MEMORY_TAILSCALE_BIN": str(dud)})
    assert rc == 0
    assert "SKIPPED" in err
