"""Phase 4 (ship run): the app serves loopback peers only. ``flask run --host
0.0.0.0`` or a WSGI server on a LAN address would otherwise expose every route
to the network: a LAN client can send ``Host: 127.0.0.1`` itself, so the Host
allowlist alone does not stop it. Every Claude call is a fake."""
from __future__ import annotations

import json

import pytest
from _helpers import CLASSIFY_JSON

import app as app_module
import claude_cli

RUN = {"problem": "Two Sum\nExample: Input: 1\nOutput: 1", "mode": "learning",
       "language": "python"}


class Recorder:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, prompt, **kwargs):
        if "Classify the following" in prompt:
            return iter([json.dumps(CLASSIFY_JSON)])
        self.calls.append(prompt)
        return iter(["# Notes\n"])


def _authed():
    return claude_cli.AuthStatus(installed=True, logged_in=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(out))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(out / "topic_index.json"))
    return out


def _client(rec):
    application = app_module.create_app(run_fn=rec, auth_probe=_authed)
    application.config.update(TESTING=True)
    return application.test_client()


@pytest.mark.parametrize("peer", ["192.168.1.50", "10.0.0.2", "203.0.113.9",
                                  "fe80::1", "::ffff:192.168.1.5", "", "not-an-ip"])
def test_requests_from_a_non_loopback_peer_are_refused(env, peer, tmp_path):
    rec = Recorder()
    application = app_module.create_app(run_fn=rec, auth_probe=_authed)
    dotenv = tmp_path / "peer.env"
    application.config.update(TESTING=True, DOTENV_PATH=str(dotenv))
    c = application.test_client()
    base = {"REMOTE_ADDR": peer}
    assert c.get("/healthz", environ_base=base).status_code == 403
    assert c.get("/", environ_base=base).status_code == 403
    assert c.post("/run", json=RUN, environ_base=base).status_code == 403
    assert c.post("/config/model", json={"model": "haiku"},
                  environ_base=base).status_code == 403
    assert rec.calls == []
    assert not dotenv.exists()


@pytest.mark.parametrize("peer", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
def test_requests_from_a_loopback_peer_are_served(env, peer):
    resp = _client(Recorder()).get("/healthz", environ_base={"REMOTE_ADDR": peer})
    assert resp.status_code == 200
