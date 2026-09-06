"""The leak scanner is ONE script shared by the pre-commit hook and CI, so the
rules local and CI enforce cannot drift. These tests pin the classes it rejects
(secrets, personal/infrastructure identifiers, and deny-listed personal
content), prove documented placeholders stay allowed, and, critically, assert
the committed tree scans clean, which is what actually enforces the policy
inside the keyless CI pytest job.

The script itself is a byte-for-byte copy of crossband's, the fleet's canonical
scanner. A pattern fix lands there first and is copied here; the last test in
this file hashes the copy against the canonical so a copy that differs is a red
build rather than a scanner that quietly misses what the others catch.

Keyless and hermetic apart from that one drift test, which reads a local
crossband checkout when there is one and otherwise downloads the canonical with
a short timeout, skipping (visibly) when it cannot.
"""
import hashlib
import os
import subprocess
import urllib.request
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
    assert "CREDENTIAL" in out


# ── the two classes stay DISTINCT (a key is not an identifier) ───────────────
def test_identifier_is_not_reported_as_secret():
    rc, out = run("--files", str(FIX / "leaky.txt"))
    assert "IDENTIFIER" in out and "CREDENTIAL" not in out


def test_secret_is_not_reported_as_identifier():
    rc, out = run("--files", str(FIX / "secret.txt"))
    assert "CREDENTIAL" in out and "IDENTIFIER" not in out


# ── per-token truth table: placeholders pass, real shapes fail ───────────────
ALLOWED = [
    "my-mac.my-tailnet.ts.net",
    "<mac>.<tailnet>.ts.net",
    "my-mac.tailXXXX.ts.net",
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
    # A masked tailnet does not launder the machine label in front of it:
    # the allowlist matches the WHOLE token, so a label that is not itself a
    # documented placeholder (my-..., <...>) still reads as a real host.
    "mymac.tailXXXX.ts.net",
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
    in RFC-shaped emails, so an unanchored local part let '+@mcp.tool' read as
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
    a real-looking home path (/Users/<name>/…, /home/runner/… in CI) must pass:
    the caller's path is not the repo's content. A sibling copy of this
    scanner once shipped with a path-prefix bug that failed every scan on a
    real machine; this pins membro against ever growing the same one."""
    home = tmp_path / "Users" / "realperson"
    home.mkdir(parents=True)
    f = home / "clean.txt"
    f.write_text("host is my-mac.my-tailnet.ts.net\n")
    rc, out = run("--files", str(f))
    assert rc == 0, f"file path leaked into scanned text:\n{out}"


# ── the gate can detect, not just pass ─────────────────────────────────
#
# `test_tree_scan_is_clean` above asserts a clean tree scans clean, which is
# EXACTLY what a --tree mode scanning zero bytes would also produce. Every other
# test drives --files or staged mode, so nothing proved the tree walk can find
# anything: the release gate could rot without a test going red. Kept in
# lockstep with the canonical's own tests.

def _planted(text):
    """Assemble leak-shaped strings at RUNTIME, never as literals. A test file
    CONTAINING a real-shaped key or home path is itself a leak the scanner would
    need taught to ignore, and an exclusion is one more thing that can silently
    stop matching. Built from fragments, this file stays clean by construction."""
    return text


FAKE_SECRET = _planted("sk-" + "ant-" + "api03-" + "B" * 32)
FAKE_HOMEPATH = _planted("/Users/" + "notarealperson")


def _tree_repo(tmp_path, contents):
    """A real git repo with `contents` COMMITTED. `git ls-files` lists only
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
    assert "CREDENTIAL" in out


def test_tree_scan_actually_detects_a_planted_identifier(tmp_path):
    rc, out = _scan_in(_tree_repo(tmp_path, f"LOG = '{FAKE_HOMEPATH}/log'"), "--tree")
    assert rc == 1, f"--tree missed a committed identifier:\n{out}"
    assert "IDENTIFIER" in out


def test_the_bare_invocation_trap_is_real_and_pinned(tmp_path):
    """The reason RELEASING.md says never to use the bare command, in executable
    form. On the SAME repo --tree rejects, no-argument mode reports clean,
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
    write the log to a file first; this proves that instruction works and that
    the piped form it warns against would not."""
    log_file = tmp_path / "hist.txt"
    log_file.write_text(f"+ KEY = '{FAKE_SECRET}'\n")
    rc, out = _scan_in(tmp_path, "--files", str(log_file))
    assert rc == 1 and "CREDENTIAL" in out
    # a path that is not a regular file is skipped: scanning nothing, reporting clean
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
    for whatever lands in it later. Exclusions can come from the script's
    built-in list or from a repo's .secret-scan-exclude, so both are read."""
    assert ":(exclude)requirements.txt" not in SCAN.read_text()
    listing = REPO / ".secret-scan-exclude"
    if listing.exists():
        entries = [ln.split("#", 1)[0].strip() for ln in listing.read_text().splitlines()]
        assert "requirements.txt" not in entries


# ── drift: this file is a copy of crossband's canonical scanner ──────────────
#
# The fleet runs ONE scanner. crossband owns it; this repo carries it byte for
# byte, so a pattern fix lands there first and is copied here. The copy is
# hashed against the canonical so a copy that differs is a red build rather
# than a scanner that quietly misses what the others catch.

CANONICAL_URL = ("https://raw.githubusercontent.com/shawn-durrani/crossband/"
                 "main/scripts/secret-scan.sh")
SYNC_HINT = (
    "scripts/secret-scan.sh is a byte-for-byte copy of crossband's canonical "
    "scanner. Pattern fixes land in crossband first; do not patch this copy. "
    "Sync it with:\n"
    f"  curl -fsSL {CANONICAL_URL} -o scripts/secret-scan.sh\n"
    "or copy the file from a local crossband checkout, then commit."
)


def _canonical_scanner():
    """The canonical's bytes and where they came from, or (None, why not).
    In order: an explicit SECRET_SCAN_CANONICAL path (which must exist), a
    sibling or home-directory crossband checkout, else a download with a
    short timeout so the suite stays runnable offline."""
    explicit = os.environ.get("SECRET_SCAN_CANONICAL")
    if explicit:
        p = Path(explicit)
        assert p.is_file(), f"SECRET_SCAN_CANONICAL={explicit} is not a file"
        return p.read_bytes(), f"SECRET_SCAN_CANONICAL={explicit}"
    for p in (REPO.parent / "crossband" / "scripts" / "secret-scan.sh",
              Path.home() / "dev" / "crossband" / "scripts" / "secret-scan.sh"):
        if p.is_file():
            return p.read_bytes(), str(p)
    try:
        with urllib.request.urlopen(CANONICAL_URL, timeout=10) as resp:
            return resp.read(), CANONICAL_URL
    except Exception as exc:  # offline, a proxy, a GitHub hiccup
        return None, f"{CANONICAL_URL} ({exc.__class__.__name__}: {exc})"


def test_scanner_matches_canonical():
    canonical, source = _canonical_scanner()
    if canonical is None:
        pytest.skip(f"the canonical scanner is not reachable: {source}. "
                    "CI has network and checks this; offline it cannot.")
    mine = hashlib.sha256(SCAN.read_bytes()).hexdigest()
    theirs = hashlib.sha256(canonical).hexdigest()
    assert mine == theirs, (
        f"scripts/secret-scan.sh has drifted from the canonical at {source}\n"
        f"  local     sha256 {mine}\n  canonical sha256 {theirs}\n{SYNC_HINT}")
