"""D16/B11: ``GET /healthz`` and single-instance reuse at launch.

A second launch must not start a second server on the next free port sharing
``output/`` (the per-process locks don't coordinate). ``main()`` first asks the
preferred port's ``/healthz``; if LeetCoach answers, it opens the browser there
and exits. LeetCoach may itself sit on a fallback port, so the probe walks the
whole fallback span (skipping free ports) within an overall time budget (SP4
fix M10). These tests use real loopback sockets (werkzeug's dev server on an
ephemeral port) and fakes for the browser and the server start - no `claude`.
"""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from leetcoach import app as app_module
from leetcoach import claude_cli, launcher


def _authed():
    return claude_cli.AuthStatus(installed=True, logged_in=True)


def _app():
    application = app_module.create_app(run_fn=lambda *a, **k: iter(()), auth_probe=_authed)
    application.config.update(TESTING=True)
    return application


# --- GET /healthz --------------------------------------------------------------

def test_healthz_identifies_leetcoach():
    resp = _app().test_client().get("/healthz")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"app": "leetcoach", "version": app_module.VERSION}
    assert data["version"]


def test_version_matches_the_latest_changelog_release():
    # The Cycle 11 release; the CHANGELOG's newest entry must name the same version.
    assert app_module.VERSION == "1.5.0"
    repo_root = Path(__file__).resolve().parent.parent
    changelog = (repo_root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    first = next(line for line in changelog.splitlines() if line.startswith("## ["))
    assert first.startswith(f"## [{app_module.VERSION}]")


def test_healthz_is_host_checked():
    resp = _app().test_client().get("/healthz", headers={"Host": "evil.example"})
    assert resp.status_code == 403


# --- probing a port ---------------------------------------------------------------

@pytest.fixture
def served():
    """Start a real WSGI server on an ephemeral loopback port; yields a
    ``start(wsgi_app) -> port`` helper and shuts everything down after."""
    servers = []

    def start(wsgi_app):
        srv = make_server("127.0.0.1", 0, wsgi_app, threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv.server_port

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_probe_finds_a_running_leetcoach(served):
    port = served(_app())
    assert launcher.existing_instance_url("127.0.0.1", port) == f"http://127.0.0.1:{port}/"


def test_probe_ignores_some_other_server(served):
    def other(environ, start_response):
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps({"app": "something-else"}).encode()]

    port = served(other)
    assert launcher.existing_instance_url("127.0.0.1", port) is None


def test_probe_ignores_a_non_json_answer(served):
    def html(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/html")])
        return [b"<html>hi</html>"]

    port = served(html)
    assert launcher.existing_instance_url("127.0.0.1", port) is None


@pytest.fixture
def garbage_listener():
    """A raw TCP listener that answers any connection with a non-HTTP line
    (like a Redis server would); yields its port."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(4096)
                    conn.sendall(b"-ERR unknown command\r\n")
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1]
    stop.set()
    srv.close()


def test_probe_of_a_non_http_listener_is_none(garbage_listener):
    # 3A W1: urllib raises http.client.BadStatusLine (an HTTPException, not an
    # OSError) for a non-HTTP reply; the probe must treat it as "not LeetCoach"
    # instead of crashing the launch with a traceback.
    assert launcher.existing_instance_url("127.0.0.1", garbage_listener, timeout=2) is None


def test_find_existing_instance_survives_a_non_http_listener(garbage_listener):
    assert launcher.find_existing_instance("127.0.0.1", garbage_listener, span=0) is None


def test_probe_of_a_closed_port_is_none():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert launcher.existing_instance_url("127.0.0.1", port, timeout=0.5) is None


def test_probe_url_brackets_ipv6_and_maps_wildcard_hosts():
    seen = []

    def fake_open(url, timeout):
        seen.append(url)
        raise OSError("no server")

    launcher.existing_instance_url("::1", 5000, opener=fake_open)
    launcher.existing_instance_url("0.0.0.0", 5000, opener=fake_open)
    assert seen == ["http://[::1]:5000/healthz", "http://127.0.0.1:5000/healthz"]


# --- main(): reuse before port fallback ---------------------------------------------

def test_main_reuses_a_running_instance_and_does_not_serve(monkeypatch):
    opened, served_ports = [], []
    monkeypatch.setattr(launcher, "port_is_free", lambda host, port: False)
    monkeypatch.setattr(launcher, "existing_instance_url",
                        lambda host, port, **kw: f"http://127.0.0.1:{port}/")

    def no_choose(*a, **k):
        raise AssertionError("must not fall back to another port")

    monkeypatch.setattr(launcher, "choose_port", no_choose)
    rc = launcher.main(open_browser=opened.append, serve=lambda port: served_ports.append(port))
    assert rc == 0
    assert opened == [f"http://127.0.0.1:{launcher.PORT}/"]
    assert served_ports == []


def test_main_reuse_respects_no_browser(monkeypatch, capsys):
    opened = []
    monkeypatch.setenv("LEETCOACH_NO_BROWSER", "1")
    monkeypatch.setattr(launcher, "port_is_free", lambda host, port: False)
    monkeypatch.setattr(launcher, "existing_instance_url",
                        lambda host, port, **kw: f"http://127.0.0.1:{port}/")
    rc = launcher.main(open_browser=opened.append, serve=lambda port: None)
    assert rc == 0
    assert opened == []
    assert "already running" in capsys.readouterr().out


def test_main_serves_when_no_instance_is_running(monkeypatch):
    served_ports = []
    monkeypatch.setenv("LEETCOACH_NO_BROWSER", "1")
    monkeypatch.setattr(launcher, "existing_instance_url", lambda host, port, **kw: None)
    monkeypatch.setattr(launcher, "choose_port", lambda preferred, host: 5007)
    monkeypatch.setattr(launcher, "sweep_sandbox_temp", lambda: 0)
    monkeypatch.setattr(app_module.storage, "migrate_tier_suffixes", list)
    rc = launcher.main(open_browser=lambda url: None, serve=served_ports.append)
    assert rc == 0
    assert served_ports == [5007]


def test_main_never_lets_flask_reload_the_dotenv(monkeypatch):
    """3A G1: ``Flask.run`` loads ``.env`` from the cwd with python-dotenv as
    strict UTF-8 by default. That crashed the launch on a UTF-16 ``.env`` (what
    Windows PowerShell 5.1's ``echo X=1 > .env`` writes), which the app's own
    loader decodes or skips with a warning (B12), and it re-read the real
    ``.env`` even under ``LEETCOACH_NO_DOTENV``. main() must opt out."""
    calls = []
    monkeypatch.setenv("LEETCOACH_NO_BROWSER", "1")
    monkeypatch.setattr(launcher, "existing_instance_url", lambda host, port, **kw: None)
    monkeypatch.setattr(launcher, "choose_port", lambda preferred, host: 5007)
    monkeypatch.setattr(launcher, "sweep_sandbox_temp", lambda: 0)
    monkeypatch.setattr(app_module.storage, "migrate_tier_suffixes", list)
    monkeypatch.setattr(app_module.app, "run", lambda *a, **k: calls.append(k))
    assert launcher.main(open_browser=lambda url: None) == 0
    assert calls and calls[0].get("load_dotenv") is False, calls


# --- M10: probe /healthz across the fallback span -------------------------------

def _fake_opener(answers, seen, clock=None, cost=0.0):
    class Resp:
        def __init__(self, body):
            self._body = body

        def read(self, n):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(url, timeout):
        seen.append((url, timeout))
        if clock is not None:
            clock[0] += cost
        port = int(url.rsplit(":", 1)[1].split("/")[0])
        body = answers.get(port)
        if body is None:
            raise OSError("connection refused")
        return Resp(json.dumps(body).encode())

    return opener


def test_finds_leetcoach_on_a_fallback_port():
    seen: list = []
    answers = {5000: {"app": "other"}, 5002: {"app": "leetcoach"}}
    url = launcher.find_existing_instance(
        "127.0.0.1", 5000, span=5, opener=_fake_opener(answers, seen),
        is_free=lambda host, port: port not in (5000, 5002),
    )
    assert url == "http://127.0.0.1:5002/"
    # free ports are never probed (nothing listens there)
    assert [u for u, _ in seen] == ["http://127.0.0.1:5000/healthz",
                                    "http://127.0.0.1:5002/healthz"]
    assert all(t <= 1.0 for _, t in seen)


def test_no_instance_when_nothing_in_the_span_is_leetcoach():
    seen: list = []
    url = launcher.find_existing_instance(
        "127.0.0.1", 5000, span=3, opener=_fake_opener({}, seen),
        is_free=lambda host, port: False,
    )
    assert url is None
    assert len(seen) == 4


def test_probing_is_bounded_by_an_overall_budget():
    clock = [0.0]
    seen: list = []
    url = launcher.find_existing_instance(
        "127.0.0.1", 5000, span=20, budget=2.0,
        opener=_fake_opener({}, seen, clock=clock, cost=0.5),
        is_free=lambda host, port: False, clock=lambda: clock[0],
    )
    assert url is None
    assert len(seen) == 4  # 4 x 0.5 s spent the 2 s budget; no more probes
    assert all(t <= 0.5 + 1e-9 or i == 0 for i, (_, t) in enumerate(seen))


def test_port_is_free_sees_a_running_werkzeug_server():
    from werkzeug.serving import make_server

    srv = make_server("127.0.0.1", 0, lambda e, s: [], threaded=True)
    try:
        assert launcher.port_is_free("127.0.0.1", srv.server_port) is False
    finally:
        srv.server_close()
    assert launcher.port_is_free("127.0.0.1", srv.server_port) is True


def test_main_uses_the_span_probe(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher, "find_existing_instance",
                        lambda host, port, **kw: "http://127.0.0.1:5003/")

    def no_choose(*a, **k):
        raise AssertionError("must not fall back to another port")

    monkeypatch.setattr(launcher, "choose_port", no_choose)
    rc = launcher.main(open_browser=opened.append, serve=lambda port: None)
    assert rc == 0
    assert opened == ["http://127.0.0.1:5003/"]
