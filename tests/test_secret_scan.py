"""The leak scanner is ONE script shared by the pre-commit hook and CI, so the
rules local and CI enforce cannot drift. These tests pin both classes it rejects
(secrets AND personal/infrastructure identifiers), prove documented placeholders
stay allowed, and — critically — assert the committed tree scans clean, which is
what actually enforces the policy inside the keyless CI pytest job.

Keyless and hermetic: the scanner is pure bash + git, no network, no API keys.
"""
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCAN = REPO / "scripts" / "secret-scan.sh"
FIX = REPO / "tests" / "fixtures" / "identifiers"


def run(*args, local_list=None):
    """Invoke the scanner; return (returncode, combined output). The
    personal-content class is pinned to an explicit deny-list path so tests
    behave identically on a machine with a real .secret-scan-local and in CI
    without one."""
    import os
    env = dict(os.environ)
    env["SECRET_SCAN_LOCAL"] = local_list or "/nonexistent"
    p = subprocess.run(
        ["bash", str(SCAN), *args],
        cwd=REPO, capture_output=True, text=True, env=env,
    )
    return p.returncode, p.stdout + p.stderr


def scan_string(tmp_path, text):
    """Write `text` to a temp file and scan it with --files."""
    f = tmp_path / "sample.txt"
    f.write_text(text)
    return run("--files", str(f))


# ── the scanner exists and is a shared, single implementation ────────────────
def test_scanner_present():
    assert SCAN.exists(), "one scanner backs both the hook and CI"


# ── fixtures: whole-file pass/fail ───────────────────────────────────────────
def test_clean_fixture_passes():
    rc, out = run("--files", str(FIX / "clean.txt"))
    assert rc == 0, f"documented placeholders must pass:\n{out}"


def test_leaky_fixture_fails():
    rc, out = run("--files", str(FIX / "leaky.txt"))
    assert rc == 1, "real-shaped identifiers must be rejected"
    assert "IDENTIFIER" in out


def test_secret_fixture_fails():
    rc, out = run("--files", str(FIX / "secret.txt"))
    assert rc == 1, "real-shaped keys must be rejected"
    assert "KEY" in out


# ── the two classes stay DISTINCT (a key is not an identifier) ───────────────
def test_identifier_is_not_reported_as_secret():
    rc, out = run("--files", str(FIX / "leaky.txt"))
    assert "IDENTIFIER" in out and "KEY" not in out


def test_secret_is_not_reported_as_identifier():
    rc, out = run("--files", str(FIX / "secret.txt"))
    assert "KEY" in out and "IDENTIFIER" not in out


# ── per-token truth table: placeholders pass, real shapes fail ───────────────
ALLOWED = [
    "my-mac.my-tailnet.ts.net",
    "<mac>.<tailnet>.ts.net",
    "mymac.tailXXXX.ts.net",
    "/Users/you",
    "/home/you",
    "/Users/<user>",
    "you@example.com",
    "alex@example.org",
    "12345+alex@users.noreply.github.com",
    "git@github.com",
    "noreply@github.com",
]

REJECTED = [
    "workstation.tail9f3d2.ts.net",   # real-shaped tailnet host
    "laptop.tail8b1e0.ts.net",
    "/Users/contributor",             # machine-specific home path
    "/home/contributor",
    "contributor@mailprovider.io",    # personal email, non-placeholder domain
    "first.last@somecompany.co",
]


@pytest.mark.parametrize("token", ALLOWED)
def test_placeholder_token_passes(tmp_path, token):
    rc, out = scan_string(tmp_path, f"see {token} in the docs\n")
    assert rc == 0, f"placeholder {token!r} should pass:\n{out}"


@pytest.mark.parametrize("token", REJECTED)
def test_real_shaped_token_fails(tmp_path, token):
    rc, out = scan_string(tmp_path, f"value = {token}\n")
    assert rc == 1, f"real-shaped {token!r} should be rejected"


# ── lockfile / dependency false positives are excluded ───────────────────────
def test_requirements_style_line_is_not_flagged(tmp_path):
    # version pins and hashes must never read as secrets
    rc, out = scan_string(tmp_path, "anthropic>=0.40 --hash=sha256:abcdef0123456789\n")
    assert rc == 0, f"dependency pins must not trip the scanner:\n{out}"


# ── THE gate: the committed tree scans clean (this is what CI enforces) ──────
def test_tree_scan_is_clean():
    rc, out = run("--tree")
    assert rc == 0, f"a secret or identifier is in the committed tree:\n{out}"


# ── a staged decorator line must never read as an email ────
def test_staged_decorator_line_is_not_flagged_as_email(tmp_path):
    """git diff prefixes every added line with a bare '+'. A module-level
    decorator like '@mcp.tool()' or '@pytest.fixture' sits flush against that
    '+' with no separating whitespace, and '+' is a legal local-part character
    in RFC-shaped emails — so an unanchored local part let '+@mcp.tool' read as
    an email-shaped identifier on the very first commit of this file. The email
    token now requires the local part to START with an alphanumeric, which a
    bare diff '+' is not."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "you@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    f = tmp_path / "sample.py"
    f.write_text("def existing():\n    pass\n")
    subprocess.run(["git", "add", "sample.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    f.write_text("def existing():\n    pass\n\n\n@mcp.tool()\ndef search_facts():\n    pass\n")
    subprocess.run(["git", "add", "sample.py"], cwd=tmp_path, check=True)

    p = subprocess.run(["bash", str(SCAN)], cwd=tmp_path, capture_output=True, text=True)
    assert p.returncode == 0, f"a bare decorator line must not be flagged:\n{p.stdout}{p.stderr}"


# ── the file's own path must never be scanned as content ─────────────────────
def test_files_path_prefix_is_not_scanned(tmp_path):
    """--files must scan file CONTENT only. A clean file that merely LIVES under
    a real-looking home path (/Users/<name>/…, /home/runner/… in CI) must pass —
    the caller's path is not the repo's content. A sibling mirror of this
    scanner shipped with a path-prefix bug that failed every scan on a
    real machine; this pins membro against ever growing the same one."""
    home = tmp_path / "Users" / "realperson"
    home.mkdir(parents=True)
    f = home / "clean.txt"
    f.write_text("host is my-mac.my-tailnet.ts.net\n")
    rc, out = run("--files", str(f))
    assert rc == 0, f"file path leaked into scanned text:\n{out}"


# ── the gate can detect, not just pass ─────────────────────────────────
#
# `test_tree_scan_is_clean` above asserts a clean tree scans clean — which is
# EXACTLY what a --tree mode scanning zero bytes would also produce. Every other
# test drives --files or staged mode, so nothing proved the tree walk can find
# anything: the release gate could rot without a test going red. Kept in
# lockstep with its sibling mirrors.

def _planted(text):
    """Assemble leak-shaped strings at RUNTIME, never as literals — a test file
    CONTAINING a real-shaped key or home path is itself a leak the scanner would
    need taught to ignore, and an exclusion is one more thing that can silently
    stop matching. Built from fragments, this file stays clean by construction."""
    return text


FAKE_SECRET = _planted("sk-" + "ant-" + "api03-" + "B" * 32)
FAKE_HOMEPATH = _planted("/Users/" + "notarealperson")


def _tree_repo(tmp_path, contents):
    """A real git repo with `contents` COMMITTED — `git ls-files` lists only
    tracked files, so an untracked plant would prove nothing."""
    repo = tmp_path / "planted"
    repo.mkdir()
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "you@example.com"],
                ["git", "config", "user.name", "Test"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "config.py").write_text(contents + "\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "plant"], cwd=repo,
                   check=True, capture_output=True)
    return repo


def _scan_in(repo, *args):
    p = subprocess.run(["bash", str(SCAN), *args], cwd=repo,
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def test_tree_scan_actually_detects_a_planted_key(tmp_path):
    rc, out = _scan_in(_tree_repo(tmp_path, f"KEY = '{FAKE_SECRET}'"), "--tree")
    assert rc == 1, f"--tree missed a committed key:\n{out}"
    assert "KEY" in out


def test_tree_scan_actually_detects_a_planted_identifier(tmp_path):
    rc, out = _scan_in(_tree_repo(tmp_path, f"LOG = '{FAKE_HOMEPATH}/log'"), "--tree")
    assert rc == 1, f"--tree missed a committed identifier:\n{out}"
    assert "IDENTIFIER" in out


def test_the_bare_invocation_trap_is_real_and_pinned(tmp_path):
    """The reason RELEASING.md says never to use the bare command, in executable
    form. On the SAME repo --tree rejects, no-argument mode reports clean —
    because at release time nothing is staged, so it inspects zero bytes. If bare
    mode ever starts scanning the tree, this fails and the warning in the docs
    becomes the stale thing."""
    repo = _tree_repo(tmp_path, f"KEY = '{FAKE_SECRET}'")
    assert _scan_in(repo, "--tree")[0] == 1     # the committed key IS there
    rc, out = _scan_in(repo)                     # …and bare mode sails past it
    assert rc == 0
    assert "clean" in out


def test_the_documented_history_scan_reads_a_real_file(tmp_path):
    """The history-scan doc fix, second half: `--files` skips anything that isn't a regular file, so
    PIPING a git log into it silently scans nothing. RELEASING.md now says to
    write the log to a file first — this proves that instruction works and that
    the piped form it warns against would not."""
    log_file = tmp_path / "hist.txt"
    log_file.write_text(f"+ KEY = '{FAKE_SECRET}'\n")
    rc, out = _scan_in(tmp_path, "--files", str(log_file))
    assert rc == 1 and "KEY" in out
    # a path that is not a regular file is skipped — scanning nothing, reporting clean
    rc, out = _scan_in(tmp_path, "--files", "/dev/stdin")
    assert rc == 0


# ── class 3: personal content from the gitignored local deny-list ───────────

def test_local_denylist_catches_personal_content(tmp_path):
    deny = tmp_path / "deny"
    deny.write_text("# a name and a private repo slug\njane citizen\nplan-repo-x\n")
    hot = tmp_path / "doc.txt"
    hot.write_text("Ask JANE CITIZEN, notes in plan-repo-x.\n")
    code, out = run("--files", str(hot), local_list=str(deny))
    assert code == 1 and "PERSONAL CONTENT" in out


def test_missing_denylist_is_skipped_loudly(tmp_path):
    ok = tmp_path / "ok.txt"
    ok.write_text("nothing personal here\n")
    code, out = run("--files", str(ok))
    assert code == 0
    assert "SKIPPED" in out, "a skipped class must say so, not imply coverage"


def test_tree_scan_clean_against_real_denylist_when_present():
    """On a machine with a real deny-list the ship set must be free of
    personal content; in CI (no file) this degrades to the plain scan."""
    real = REPO / ".secret-scan-local"
    code, out = run("--tree", local_list=str(real) if real.exists() else None)
    assert code == 0, out


# ── published files are never pre-exempt ─────────────────────────────────────
def test_requirements_txt_is_not_exempt():
    """requirements.txt is tracked and ships, so it must face every matcher.
    The exclusion existed for lockfile noise the file never contained, and
    the pin-with-hash test above proves the matchers ignore that noise
    anyway. A published file on the exclude list is a silent pre-exemption
    for whatever lands in it later."""
    text = (REPO / "scripts" / "secret-scan.sh").read_text()
    assert ":(exclude)requirements.txt" not in text
