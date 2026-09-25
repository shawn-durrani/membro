"""The admin header's row of links to the owner's other apps (workbench#100).

Pinned here: membro's health route says where a browser opens it; the probe
of the other apps leaves out any that don't answer, only talks to this
machine, and asks at most once a minute; the row shows tailnet links on a
tailnet page and loopback links on this machine; and the route that hands
the row to the page is owner-only.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memory_service import app_links
from memory_service.api import create_app
from memory_service.config import DEFAULT_SIBLING_APPS, Settings, load_settings

TAILNET = "my-mac.my-tailnet.ts.net"
SIBLINGS = [
    {"name": "crossband", "origin": f"https://{TAILNET}", "local": "http://127.0.0.1:8902"},
    {"name": "spendglass", "origin": "http://127.0.0.1:8903", "local": "http://127.0.0.1:8903"},
    {"name": "threadfold", "origin": "", "local": "http://127.0.0.1:8904"},
]


# ---- membro's own address, on its health route ----

def test_health_carries_the_browser_origin(settings):
    h = TestClient(create_app(settings), base_url="http://127.0.0.1").get("/v1/health")
    assert h.json()["browser_origin"] == "http://127.0.0.1:8901"


# ---- config ----

def test_siblings_default_to_the_fleet_and_env_can_empty_them(monkeypatch):
    monkeypatch.delenv("MEMORY_SIBLING_APPS", raising=False)
    assert Settings().sibling_apps == DEFAULT_SIBLING_APPS
    monkeypatch.setenv("MEMORY_SIBLING_APPS", "{}")
    assert load_settings().sibling_apps == {}
    monkeypatch.setenv("MEMORY_SIBLING_APPS", "not json")
    assert isinstance(load_settings().sibling_apps, dict)


# ---- the probe ----

class _Health(BaseHTTPRequestHandler):
    status = 200
    body = {"status": "ok", "browser_origin": f"https://{TAILNET}"}

    def do_GET(self):
        raw = json.dumps(self.body).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class _Broken(_Health):
    status = 500


@pytest.fixture
def serve():
    servers = []

    def start(handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield start
    for srv in servers:
        srv.shutdown()


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_siblings_that_dont_answer_are_left_out(serve):
    up = serve(_Health)
    broken = serve(_Broken)
    probe = app_links.SiblingProbe({
        "crossband": up + "/api/auth/session",
        "spendglass": f"http://127.0.0.1:{_closed_port()}/api/session",
        "threadfold": broken + "/health",
    })
    assert probe.found() == [{"name": "crossband", "origin": f"https://{TAILNET}",
                              "local": up}]


def test_answers_are_kept_for_a_minute():
    calls, now = [], [1000.0]

    def fetch(url, timeout):
        calls.append(url)
        return {"browser_origin": ""}
    probe = app_links.SiblingProbe({"crossband": DEFAULT_SIBLING_APPS["crossband"]},
                                   fetch=fetch, clock=lambda: now[0])
    probe.found()
    now[0] += 59
    probe.found()
    assert len(calls) == 1
    now[0] += 2
    probe.found()
    assert len(calls) == 2


def test_a_slow_sibling_is_left_out_within_the_timeout():
    def fetch(url, timeout):
        time.sleep(2)
        return {"browser_origin": ""}
    probe = app_links.SiblingProbe({"crossband": DEFAULT_SIBLING_APPS["crossband"]},
                                   fetch=fetch, timeout=0.2)
    started = time.monotonic()
    assert probe.found() == []
    assert time.monotonic() - started < 1.5


def test_only_loopback_addresses_are_ever_probed():
    calls = []
    probe = app_links.SiblingProbe(
        {"elsewhere": "http://192.0.2.10:8902/api/auth/session",
         "tailnet": f"https://{TAILNET}/api/auth/session"},
        fetch=lambda url, timeout: calls.append(url))
    assert probe.found() == []
    assert calls == []


def test_a_reported_address_that_isnt_a_plain_origin_is_dropped():
    probe = app_links.SiblingProbe(
        {"crossband": DEFAULT_SIBLING_APPS["crossband"]},
        fetch=lambda url, timeout: {"browser_origin": "javascript:alert(1)"})
    assert probe.found()[0]["origin"] == ""


# ---- which links a page shows ----

def test_on_this_machine_every_sibling_opens_at_its_loopback_address():
    assert app_links.link_row("127.0.0.1", "membro", SIBLINGS) == [
        {"name": "crossband", "href": "http://127.0.0.1:8902/", "current": False},
        {"name": "membro", "href": "", "current": True},
        {"name": "spendglass", "href": "http://127.0.0.1:8903/", "current": False},
        {"name": "threadfold", "href": "http://127.0.0.1:8904/", "current": False},
    ]


def test_a_localhost_page_keeps_the_name_for_its_passkeys():
    row = app_links.link_row("localhost", "membro", SIBLINGS)
    assert row[0]["href"] == "http://localhost:8902/"


def test_on_the_tailnet_only_siblings_served_there_show():
    assert app_links.link_row(TAILNET, "membro", SIBLINGS) == [
        {"name": "crossband", "href": f"https://{TAILNET}/", "current": False},
        {"name": "membro", "href": "", "current": True},
    ]


def test_no_sibling_that_can_open_means_no_row():
    assert app_links.link_row(TAILNET, "membro", SIBLINGS[1:]) == []
    assert app_links.link_row("127.0.0.1", "membro", []) == []


def test_the_current_app_is_never_linked():
    row = app_links.link_row("127.0.0.1", "membro", SIBLINGS + [
        {"name": "membro", "origin": "", "local": "http://127.0.0.1:8901"}])
    assert [r for r in row if r["name"] == "membro"] == [
        {"name": "membro", "href": "", "current": True}]


# ---- the route the page reads ----

def _app(tmp_path, **extra):
    app = create_app(Settings(data_dir=tmp_path / "d", trusted_hosts=[TAILNET],
                              sibling_apps={}, **extra))
    app.state.sibling_probe = app_links.SiblingProbe(
        DEFAULT_SIBLING_APPS,
        fetch=lambda url, timeout: {"browser_origin": f"https://{TAILNET}"}
        if ":8902" in url else None)
    return app


def _signed_in(app, base_url):
    c = TestClient(app, base_url=base_url)
    app.state.admin_sessions["test-session-id"] = 1e12
    c.cookies.set("mm_admin", "test-session-id")
    return c


def test_route_is_owner_only_even_on_loopback(tmp_path):
    app = _app(tmp_path)
    assert TestClient(app, base_url="http://127.0.0.1").get("/app-links").status_code == 401
    assert TestClient(app, base_url=f"https://{TAILNET}").get("/app-links").status_code == 403


def test_route_builds_the_row_for_the_address_the_page_was_opened_at(tmp_path):
    app = _app(tmp_path)
    on_mac = _signed_in(app, "http://127.0.0.1").get("/app-links").json()["links"]
    assert [link["href"] for link in on_mac] == ["http://127.0.0.1:8902/", ""]
    on_phone = _signed_in(app, f"https://{TAILNET}").get("/app-links").json()["links"]
    assert [link["href"] for link in on_phone] == [f"https://{TAILNET}/", ""]


def test_the_page_draws_the_row():
    page = (Path(app_links.__file__).parent / "static" / "index.html").read_text()
    assert 'id="app-links"' in page
    assert 'fetch("/app-links")' in page
