"""The benchmark harness's proof that its target is a throwaway membro.

The harness and the service each read the disposability sentinel, and they
must agree on every input, or the harness could accept a store the service
won't vouch for. These tests hold both readers to the same verdicts, and
check each refusal the harness makes before it writes anything.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bench_memory import safety
from memory_service import api

ACK = {safety.DISPOSABLE_ACK_ENV: "1"}
URL = "http://127.0.0.1:18999"


def test_sentinel_contract_matches_the_service():
    assert safety.DISPOSABLE_MARKER_NAME == api.DISPOSABLE_SENTINEL_NAME
    assert safety.DISPOSABLE_MARKER_MAX_BYTES == api._SENTINEL_READ_CAP


def _hostile_sentinels(tmp_path):
    """Each case writes a sentinel shape into its own folder."""
    cases = {}

    def case(name, make):
        d = tmp_path / name
        d.mkdir()
        make(d / safety.DISPOSABLE_MARKER_NAME)
        cases[name] = d

    case("valid", lambda p: p.write_text("tok-123"))
    case("padded", lambda p: p.write_text("  tok-123\n"))
    case("empty", lambda p: p.write_text(""))
    case("blank", lambda p: p.write_text("   \n"))
    case("at_cap", lambda p: p.write_bytes(b"a" * safety.DISPOSABLE_MARKER_MAX_BYTES))
    case("over_cap", lambda p: p.write_bytes(b"a" * (safety.DISPOSABLE_MARKER_MAX_BYTES + 1)))
    case("not_utf8", lambda p: p.write_bytes(b"\xff\xfe\xfd"))
    case("directory", lambda p: p.mkdir())
    secret = tmp_path / "secret.txt"
    secret.write_text("not a token")
    case("symlink", lambda p: p.symlink_to(secret))
    if hasattr(os, "mkfifo"):
        case("fifo", lambda p: os.mkfifo(p))
    missing = tmp_path / "missing"
    missing.mkdir()
    cases["missing"] = missing
    return cases


def test_harness_and_service_agree_on_every_sentinel(tmp_path):
    for name, folder in _hostile_sentinels(tmp_path).items():
        ours = safety.read_marker_token(folder / safety.DISPOSABLE_MARKER_NAME)
        theirs = api._read_disposable_token(folder)
        assert ours == theirs, name
    valid = tmp_path / "valid" / safety.DISPOSABLE_MARKER_NAME
    assert safety.read_marker_token(valid) == "tok-123"
    assert safety.read_marker_token(tmp_path / "symlink" / safety.DISPOSABLE_MARKER_NAME) is None
    assert safety.read_marker_token(tmp_path / "over_cap" / safety.DISPOSABLE_MARKER_NAME) is None


def _marked(tmp_path, token="tok-abc"):
    d = tmp_path / "data"
    d.mkdir()
    (d / safety.DISPOSABLE_MARKER_NAME).write_text(token)
    return d


def test_local_half_needs_the_acknowledgement(tmp_path):
    with pytest.raises(safety.UnsafeLiveTarget, match=safety.DISPOSABLE_ACK_ENV):
        safety.require_disposable_target(_marked(tmp_path), base_url=URL, env={})


def test_local_half_needs_an_endpoint(tmp_path):
    with pytest.raises(safety.UnsafeLiveTarget):
        safety.require_disposable_target(_marked(tmp_path), base_url=None, env=ACK)


def test_local_half_refuses_a_real_looking_store(tmp_path):
    d = _marked(tmp_path)
    (d / "memory.db").write_text("")
    with pytest.raises(safety.UnsafeLiveTarget, match="memory.db"):
        safety.require_disposable_target(d, base_url=URL, env=ACK)


def test_local_half_refuses_a_folder_with_no_sentinel(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    with pytest.raises(safety.UnsafeLiveTarget, match="sentinel"):
        safety.require_disposable_target(d, base_url=URL, env=ACK)


def test_a_built_store_passes_only_with_its_sentinel(tmp_path):
    d = _marked(tmp_path)
    (d / "memory.db").write_text("")
    target = safety.require_disposable_target(d, base_url=URL, env=ACK,
                                              allow_existing_store=True)
    assert target.token == "tok-abc"
    (d / safety.DISPOSABLE_MARKER_NAME).unlink()
    with pytest.raises(safety.UnsafeLiveTarget):
        safety.require_disposable_target(d, base_url=URL, env=ACK,
                                         allow_existing_store=True)


def _target(tmp_path):
    return safety.require_disposable_target(_marked(tmp_path), base_url=URL, env=ACK)


@pytest.mark.parametrize("reply", [
    {"disposable": False},
    {"disposable": True},
    {"disposable": True, "token": ""},
    {"disposable": True, "token": "someone-else"},
    ["not", "an", "object"],
])
def test_endpoint_half_refuses_anything_but_our_token(tmp_path, reply):
    target = _target(tmp_path)
    with pytest.raises(safety.UnsafeLiveTarget):
        safety.verify_endpoint_is_disposable(target, fetch=lambda *_: reply)


def test_endpoint_half_accepts_our_token(tmp_path):
    target = _target(tmp_path)
    safety.verify_endpoint_is_disposable(
        target, fetch=lambda *_: {"disposable": True, "token": "tok-abc"})


def test_identity_probe_never_leaves_loopback():
    with pytest.raises(safety.UnsafeLiveTarget, match="loopback"):
        safety._fetch_identity("http://example.com:8901", 1.0)


def test_the_service_echoes_the_sentinel_it_serves(settings):
    settings.data_dir.mkdir(parents=True)
    client = TestClient(api.create_app(settings), base_url="http://127.0.0.1")
    assert client.get("/v1/disposable-identity").json() == {"disposable": False}
    (settings.data_dir / safety.DISPOSABLE_MARKER_NAME).write_text("tok-live")
    assert client.get("/v1/disposable-identity").json() == {
        "disposable": True, "token": "tok-live"}


def test_the_harness_never_imports_the_service():
    """The harness proves things about membro from outside. Importing the
    service would read its config and could open its database."""
    code = ("import sys, bench_memory.cli, bench_memory.live.run, "
            "bench_memory.live.meter, bench_memory.longmemeval.judge; "
            "print(any(m.startswith('memory_service') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=Path(__file__).resolve().parents[1])
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
