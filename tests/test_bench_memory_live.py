"""The throwaway pair's guards: environment, worktrees, teardown and replies.

None of these tests starts a service. They pin the rules that keep a
benchmark run away from your real keys, config and data.
"""

import subprocess

import pytest

from bench_memory.live import env_allowlist as ea
from bench_memory.live import launcher, session
from bench_memory.live import worktree as wt
from bench_memory.safety import DISPOSABLE_MARKER_NAME, UnsafeLiveTarget

KEYS = {"ANTHROPIC_API_KEY": "k-a", "OPENAI_API_KEY": "k-o"}
DIRTY = {
    "PATH": "/usr/bin", "LANG": "en_AU.UTF-8",
    "GITHUB_TOKEN": "x", "TAVILY_API_KEY": "x", "ELEVENLABS_API_KEY": "x",
    "MEMORY_MIRROR_DIR": "/real/mirror", "MEMORY_DATA_DIR": "/real/data",
    "MEMORY_AUTH_TOKEN": "real-token", "CROSSBAND_MEMORY_URL": "http://127.0.0.1:8901",
    "CROSSBAND_TRUSTED_HOSTS": "my-mac.my-tailnet.ts.net",
    "ANTHROPIC_BASE_URL": "https://elsewhere",
    "ANTHROPIC_API_KEY": "inherited-should-not-pass", "SSH_AUTH_SOCK": "/tmp/agent",
}


def test_membro_env_holds_only_what_it_names():
    env = ea.membro_env(data_dir="/tmp/d", port=18001, home="/tmp/h",
                        auth_token="t", meter_prefix="http://127.0.0.1:9/q1",
                        credentials=KEYS, source_env=DIRTY)
    assert env["MEMORY_DATA_DIR"] == "/tmp/d"
    assert env["MEMORY_AUTH_TOKEN"] == "t"
    assert env["MEMORY_SIBLING_APPS"] == "{}"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9/q1.membro/anthropic"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:9/q1.membro/openai/v1"
    assert env["ANTHROPIC_API_KEY"] == "k-a"
    assert env["HOME"] == "/tmp/h"
    for gone in ("GITHUB_TOKEN", "TAVILY_API_KEY", "ELEVENLABS_API_KEY",
                 "MEMORY_MIRROR_DIR", "SSH_AUTH_SOCK", "CROSSBAND_TRUSTED_HOSTS"):
        assert gone not in env


def test_crossband_env_points_at_the_throwaway_membro():
    env = ea.crossband_env(data_dir="/tmp/c", port=18002,
                           memory_url="http://127.0.0.1:18001", home="/tmp/h",
                           memory_token="t", meter_prefix="http://127.0.0.1:9/q1",
                           credentials=KEYS, anthropic_model="claude-haiku-4-5",
                           source_env=DIRTY)
    assert env["CROSSBAND_MEMORY_URL"] == "http://127.0.0.1:18001"
    assert env["CROSSBAND_DATA_DIR"] == "/tmp/c"
    assert env["MEMORY_AUTH_TOKEN"] == "t"
    assert env["CROSSBAND_ANTHROPIC_MODEL"] == "claude-haiku-4-5"
    assert env["ANTHROPIC_BASE_URL"].endswith("/q1.crossband/anthropic")
    assert "CROSSBAND_TRUSTED_HOSTS" not in env
    assert not any(k.startswith("MMC_") for k in env)


def test_an_excluded_key_can_never_be_forced_in():
    with pytest.raises(ea.EnvContract):
        ea.build_child_env(forced={"GITHUB_TOKEN": "x"}, home="/tmp/h",
                           credentials={}, source_env={})


def test_an_identity_var_arriving_unforced_is_refused(monkeypatch):
    # Guards against a later change widening the process basics.
    monkeypatch.setattr(ea, "_PROCESS_BASICS", ("PATH", "MEMORY_DATA_DIR"))
    with pytest.raises(ea.EnvContract, match="MEMORY_DATA_DIR"):
        ea.build_child_env(forced={}, home="/tmp/h", credentials={},
                           source_env=DIRTY)


def _git_repo(path):
    path.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (path / "requirements.txt").write_text("fastapi\n")
    (path / ".gitignore").write_text(".env\nconfig.local.json\n")
    run("add", ".")
    run("commit", "-qm", "init")
    return path


def test_worktree_round_trip(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    dest, commit = wt.add(repo, tmp_path / "tree", "HEAD")
    assert (dest / "requirements.txt").exists() and len(commit) == 40
    wt.write_config(dest, {"trusted_apps": ["x"]})
    assert (dest / "config.local.json").exists()
    with pytest.raises(wt.WorktreeError):
        wt.write_config(dest, {})   # already carries a config
    wt.remove(repo, dest)
    assert not dest.exists()


def test_worktree_refuses_a_path_inside_a_checkout(tmp_path):
    with pytest.raises(wt.WorktreeError):
        wt.assert_outside_live_checkouts(tmp_path / "repo" / "x", tmp_path / "repo")


def test_worktree_refuses_real_config(tmp_path):
    (tmp_path / ".env").write_text("")
    with pytest.raises(wt.WorktreeError, match=".env"):
        wt.assert_clean(tmp_path)


def test_worktree_refuses_a_venv_built_for_other_requirements(tmp_path):
    live, tree = tmp_path / "live", tmp_path / "tree"
    live.mkdir(), tree.mkdir()
    (live / "requirements.txt").write_text("fastapi\n")
    (tree / "requirements.txt").write_text("fastapi\nnew-thing\n")
    with pytest.raises(wt.WorktreeError):
        wt.assert_requirements_match(live, tree)


def test_free_ports_skip_the_fleet():
    for _ in range(20):
        assert launcher.pick_free_port() not in launcher.RESERVED_PORTS
    assert {8901, 8902, 8903, 8904} <= launcher.RESERVED_PORTS


def test_a_real_looking_data_folder_is_refused(tmp_path):
    (tmp_path / "chat.db").write_text("")
    with pytest.raises(launcher.LaunchError):
        launcher.assert_dir_is_not_real(tmp_path)


def test_teardown_deletes_only_a_tree_that_proves_itself(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / DISPOSABLE_MARKER_NAME).write_text("run-token")
    with pytest.raises(UnsafeLiveTarget):
        launcher.remove_disposable_tree(root, "another-token")
    assert root.exists()
    launcher.remove_disposable_tree(root, "run-token")
    assert not root.exists()


def test_requests_stay_on_loopback():
    with pytest.raises(launcher.LaunchError):
        launcher.http_json("http://example.com/api/state")


def test_the_membro_config_trusts_crossband():
    assert launcher.MEMBRO_CONFIG == {"trusted_apps": ["multi-model-chat"]}


@pytest.mark.parametrize("payload, want", [
    ({"messages": [{"speaker": "user", "content": "q?"},
                   {"speaker": "claude", "content": "the answer"}]}, "the answer"),
    ([{"speaker": "user", "content": "q?"},
      {"speaker": "claude", "content": "bare list"}], "bare list"),
    ({"messages": [{"speaker": "user", "content": "q?"},
                   {"speaker": "claude", "content": "   "}]}, None),
    ({"messages": [{"role": "assistant", "content": "no speaker",
                    "speaker": "user"}]}, None),
    ("not json", None),
])
def test_the_reply_is_never_the_question(payload, want):
    reply = session.pick_reply(payload)
    assert (reply or {}).get("content") == want


def test_a_failed_round_is_not_an_answer():
    stream = ('data: {"type": "token", "speaker": "claude", "text": "half"}\n\n'
              'data: {"type": "error", "speaker": "claude", "message": "overloaded"}\n\n'
              'data: not json\n\n')
    assert session.round_errors(stream) == ["overloaded"]
    assert session.round_errors('data: {"type": "done"}\n\n') == []
