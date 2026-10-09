"""Phase 4 (ship run): every /run and Quick Ask spends the user's Claude
subscription, so the number of `claude` calls they can have in flight at once
is capped. A request past the cap is a 429 and starts nothing. Every Claude
call is a fake."""
from __future__ import annotations

import json
import threading
import time

import pytest
from _helpers import CLASSIFY_JSON

import app as app_module
import claude_cli


def _authed():
    return claude_cli.AuthStatus(installed=True, logged_in=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(out))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(out / "topic_index.json"))
    return out


class Blocking:
    """A fake run that yields one delta, then waits until released or cancelled."""

    def __init__(self, release: threading.Event):
        self.release = release
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self._sent = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._sent:
            self._sent = True
            self.started.set()
            return "partial "
        while not self.release.is_set():
            if self.cancelled.wait(0.02):
                raise claude_cli.ClaudeCancelledError("cancelled")
        raise StopIteration

    def cancel(self):
        self.cancelled.set()


def _client():
    release = threading.Event()
    runs: list[Blocking] = []

    def run_fn(prompt, **kwargs):
        if "Classify the following" in prompt:
            return iter([json.dumps(CLASSIFY_JSON)])
        r = Blocking(release)
        runs.append(r)
        return r

    application = app_module.create_app(run_fn=run_fn, auth_probe=_authed)
    application.config.update(TESTING=True)
    return application.test_client(), runs, release


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _run_body(n: int) -> dict:
    return {"problem": f"Problem {n}\nInput: 1\nOutput: 1", "mode": "learning",
            "language": "python", "run_id": f"run-{n}"}


def test_runs_past_the_concurrency_cap_are_refused(env, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_CONCURRENT_RUNS", 2)
    c, runs, release = _client()
    bodies: dict[int, str] = {}
    threads = []
    for n in (1, 2):
        t = threading.Thread(
            target=lambda n=n: bodies.setdefault(
                n, c.post("/run", json=_run_body(n)).get_data(as_text=True)),
            daemon=True)
        t.start()
        threads.append(t)
    assert _wait(lambda: len(runs) == 2 and all(r.started.is_set() for r in runs))

    third = c.post("/run", json=_run_body(3))
    assert third.status_code == 429
    assert "runs are already in progress" in third.get_json()["error"]
    assert len(runs) == 2  # nothing was started for the refused run

    # cancelling one frees a slot straight away
    assert c.post("/run/cancel", json={"run_id": "run-1"}).get_json() == {"cancelled": True}
    again = c.post("/run", json=_run_body(4), buffered=False)
    assert again.status_code == 200
    release.set()
    again.get_data()
    for t in threads:
        t.join(10)
        assert not t.is_alive()


def test_quick_asks_past_the_concurrency_cap_are_refused(env, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_CONCURRENT_ASKS", 1)
    c, runs, release = _client()
    out = {}
    t = threading.Thread(
        target=lambda: out.setdefault(
            "first", c.post("/ask", json={"question": "heapq?", "ask_id": "a1"})),
        daemon=True)
    t.start()
    assert _wait(lambda: len(runs) == 1 and runs[0].started.is_set())

    second = c.post("/ask", json={"question": "bisect?"})
    assert second.status_code == 429
    assert "Quick Ask" in second.get_json()["error"]
    assert len(runs) == 1

    release.set()
    t.join(10)
    assert out["first"].status_code == 200
    # the slot is free again once the first answer is back
    ok = c.post("/ask", json={"question": "deque?"})
    assert ok.status_code == 200


def test_default_caps_leave_room_for_normal_use():
    # several tabs may stream at once; a runaway loop may not.
    assert 2 <= app_module.MAX_CONCURRENT_RUNS <= 8
    assert 2 <= app_module.MAX_CONCURRENT_ASKS <= 8
