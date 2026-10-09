"""D16/B11: ``GET /healthz`` and single-instance reuse at launch.

A second launch must not start a second server on the next free port sharing
``output/`` (the per-process locks don't coordinate). ``main()`` first asks the
preferred port's ``/healthz``; if LeetCoach answers, it opens the browser there
and exits. These tests use real loopback sockets (werkzeug's dev server on an
ephemeral port) and fakes for the browser and the server start - no `claude`.
"""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

import app as app_module
import claude_cli


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
    changelog = (Path(app_module.__file__).parent / "CHANGELOG.md").read_text(encoding="utf-8")
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
    assert app_module._existing_instance_url("127.0.0.1", port) == f"http://127.0.0.1:{port}/"


def test_probe_ignores_some_other_server(served):
    def other(environ, start_response):
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps({"app": "something-else"}).encode()]

    port = served(other)
    assert app_module._existing_instance_url("127.0.0.1", port) is None


def test_probe_ignores_a_non_json_answer(served):
    def html(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/html")])
        return [b"<html>hi</html>"]

    port = served(html)
    assert app_module._existing_instance_url("127.0.0.1", port) is None


def test_probe_of_a_closed_port_is_none():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert app_module._existing_instance_url("127.0.0.1", port, timeout=0.5) is None


def test_probe_url_brackets_ipv6_and_maps_wildcard_hosts():
    seen = []

    def fake_open(url, timeout):
        seen.append(url)
        raise OSError("no server")

    app_module._existing_instance_url("::1", 5000, opener=fake_open)
    app_module._existing_instance_url("0.0.0.0", 5000, opener=fake_open)
    assert seen == ["http://[::1]:5000/healthz", "http://127.0.0.1:5000/healthz"]


# --- main(): reuse before port fallback ---------------------------------------------

def test_main_reuses_a_running_instance_and_does_not_serve(monkeypatch):
    opened, served_ports = [], []
    monkeypatch.setattr(app_module, "_port_is_free", lambda host, port: False)
    monkeypatch.setattr(app_module, "_existing_instance_url",
                        lambda host, port, **kw: f"http://127.0.0.1:{port}/")

    def no_choose(*a, **k):
        raise AssertionError("must not fall back to another port")

    monkeypatch.setattr(app_module, "_choose_port", no_choose)
    rc = app_module.main(open_browser=opened.append, serve=lambda port: served_ports.append(port))
    assert rc == 0
    assert opened == [f"http://127.0.0.1:{app_module.PORT}/"]
    assert served_ports == []


def test_main_reuse_respects_no_browser(monkeypatch, capsys):
    opened = []
    monkeypatch.setenv("LEETCOACH_NO_BROWSER", "1")
    monkeypatch.setattr(app_module, "_port_is_free", lambda host, port: False)
    monkeypatch.setattr(app_module, "_existing_instance_url",
                        lambda host, port, **kw: f"http://127.0.0.1:{port}/")
    rc = app_module.main(open_browser=opened.append, serve=lambda port: None)
    assert rc == 0
    assert opened == []
    assert "already running" in capsys.readouterr().out


def test_main_serves_when_no_instance_is_running(monkeypatch):
    served_ports = []
    monkeypatch.setenv("LEETCOACH_NO_BROWSER", "1")
    monkeypatch.setattr(app_module, "_existing_instance_url", lambda host, port, **kw: None)
    monkeypatch.setattr(app_module, "_choose_port", lambda preferred, host: 5007)
    monkeypatch.setattr(app_module, "_sweep_sandbox_temp", lambda: 0)
    monkeypatch.setattr(app_module.storage, "migrate_tier_suffixes", list)
    rc = app_module.main(open_browser=lambda url: None, serve=served_ports.append)
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
    monkeypatch.setattr(app_module, "_existing_instance_url", lambda host, port, **kw: None)
    monkeypatch.setattr(app_module, "_choose_port", lambda preferred, host: 5007)
    monkeypatch.setattr(app_module, "_sweep_sandbox_temp", lambda: 0)
    monkeypatch.setattr(app_module.storage, "migrate_tier_suffixes", list)
    monkeypatch.setattr(app_module.app, "run", lambda *a, **k: calls.append(k))
    assert app_module.main(open_browser=lambda url: None) == 0
    assert calls and calls[0].get("load_dotenv") is False, calls
