"""B1: the `claude auth status` probe is bounded, decodes UTF-8, and is cached.

* A hung probe used to kill only the ``cmd.exe`` shim (the real ``node``
  child - or anything holding the pipe - kept ``subprocess.run`` waiting the
  whole 15 s timeout and beyond). The probe now runs through the bounded,
  tree-killing runner.
* Output was decoded with the console code page, so a non-Latin account name
  turned "signed in" into a decode error -> "signed out".
* It ran synchronously on EVERY ``GET /``; it is now cached (60 s for a
  signed-in result, refreshed in the background once stale).

No real `claude` is spawned: this Python interpreter stands in for it. The
real runner is captured at import time, before the suite-wide autouse fixture
(tests/conftest.py) swaps in a canned one.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import pytest
from _helpers import pid_alive, wait_dead

import app as app_module
import claude_cli

REAL_AUTH_RUNNER = claude_cli._default_auth_runner


def test_auth_probe_decodes_utf8_non_latin_account_names():
    # U+3041 / U+0410 encode to UTF-8 bytes (0x81, 0x90) that cp1252 cannot
    # decode at all - the old console-code-page read turned this into an error.
    script = (
        "import json, sys\n"
        "payload = {'loggedIn': True, 'email': 'user@example.com',\n"
        "           'orgName': '\\u3041\\u3042 \\u0410\\u0411 caf\\u00e9'}\n"
        "sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))\n"
    )

    def runner(argv):
        return REAL_AUTH_RUNNER([sys.executable, "-c", script])

    status = claude_cli.auth_status(run=runner, which=lambda name: "claude")
    assert status == claude_cli.AuthStatus(installed=True, logged_in=True)


def test_auth_probe_timeout_kills_the_whole_tree(monkeypatch):
    monkeypatch.setattr(claude_cli, "AUTH_PROBE_TIMEOUT", 1.0)
    script = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'],\n"
        "                     stdin=subprocess.DEVNULL, stdout=sys.stdout,\n"
        "                     stderr=subprocess.DEVNULL)\n"
        "sys.stderr.write('GRANDCHILD %d\\n' % g.pid)\n"
        "sys.stderr.flush()\n"
        "time.sleep(40)\n"
    )
    out: dict = {}

    def go():
        try:
            REAL_AUTH_RUNNER([sys.executable, "-c", script])
        except BaseException as exc:  # noqa: BLE001 - inspected below
            out["exc"] = exc

    start = time.monotonic()
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(20)
    elapsed = time.monotonic() - start
    assert not t.is_alive(), "auth probe hung past its timeout (only the shim was killed?)"
    assert isinstance(out.get("exc"), subprocess.TimeoutExpired)
    assert elapsed < 12


def test_auth_probe_timeout_reaps_pipe_holding_grandchild(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_cli, "AUTH_PROBE_TIMEOUT", 1.0)
    pid_file = tmp_path / "gpid"
    script = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'],\n"
        "                     stdin=subprocess.DEVNULL, stdout=sys.stdout,\n"
        "                     stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(g.pid))\n"
        "time.sleep(40)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        REAL_AUTH_RUNNER([sys.executable, "-c", script])
    gpid = int(pid_file.read_text())
    try:
        assert wait_dead(gpid), "the probe's grandchild survived the timeout kill"
    finally:
        if pid_alive(gpid):
            os.kill(gpid, signal.SIGTERM)


def test_auth_probe_timeout_reports_installed_but_not_signed_in(monkeypatch):
    def hung(argv):
        raise subprocess.TimeoutExpired(argv, 1)

    status = claude_cli.auth_status(run=hung, which=lambda name: "claude")
    assert status == claude_cli.AuthStatus(installed=True, logged_in=False)


def test_auth_probe_runs_in_the_neutral_cwd(monkeypatch, tmp_path):
    seen = {}

    def fake_bounded(argv, *, timeout, cwd=None):
        seen["cwd"] = cwd
        return 0, '{"loggedIn": true}'

    monkeypatch.setattr(claude_cli, "_run_bounded", fake_bounded)
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", str(tmp_path / "neutral"))
    # A name that resolves to nothing: even a regressed runner that bypasses
    # _run_bounded could never reach a real CLI from here.
    proc = REAL_AUTH_RUNNER(["leetcoach-no-such-claude-binary", "auth", "status"])
    assert proc.stdout == '{"loggedIn": true}'
    assert seen["cwd"] == str(tmp_path / "neutral")


# --- 60 s cache --------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(claude_cli, "_monotonic", c)
    claude_cli.clear_auth_cache()
    yield c
    claude_cli.clear_auth_cache()


def _counting_probe(monkeypatch, result):
    calls = []

    def fake_auth_status(**kwargs):
        calls.append(1)
        return result

    monkeypatch.setattr(claude_cli, "auth_status", fake_auth_status)
    return calls


def test_cached_auth_status_probes_once_within_ttl(monkeypatch, clock):
    calls = _counting_probe(monkeypatch, claude_cli.AuthStatus(True, True))
    for _ in range(5):
        assert claude_cli.cached_auth_status() == claude_cli.AuthStatus(True, True)
        clock.now += 10
    assert len(calls) == 1


def test_stale_signed_in_result_is_served_while_refreshing_in_background(monkeypatch, clock):
    calls = _counting_probe(monkeypatch, claude_cli.AuthStatus(True, True))
    claude_cli.cached_auth_status()
    clock.now += claude_cli.AUTH_CACHE_TTL + 1
    assert claude_cli.cached_auth_status() == claude_cli.AuthStatus(True, True)
    deadline = time.monotonic() + 5
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(calls) == 2  # refreshed off the request path


def test_signed_out_result_is_rechecked_quickly(monkeypatch, clock):
    # After `claude auth login` the user reloads: a negative result must not
    # stick for a full minute.
    calls = _counting_probe(monkeypatch, claude_cli.AuthStatus(True, False))
    claude_cli.cached_auth_status()
    clock.now += claude_cli.AUTH_NEGATIVE_TTL + 0.5
    claude_cli.cached_auth_status()
    assert len(calls) == 2


def test_index_uses_the_cached_probe_by_default(monkeypatch, clock):
    calls = _counting_probe(monkeypatch, claude_cli.AuthStatus(True, True))
    client = app_module.create_app(run_fn=lambda *a, **k: iter(())).test_client()
    for _ in range(4):
        assert client.get("/").status_code == 200
    assert len(calls) == 1
