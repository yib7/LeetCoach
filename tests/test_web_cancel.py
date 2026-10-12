"""Stopping work in flight. ``POST /run/cancel``: a Stop before vs. after the
run commits to saving (M1), during verification (M2), the ``cancelled`` event
(B2), a cancel that itself fails (M12) and the page's Stop wiring.
``POST /ask/cancel`` for Quick Ask (B20, R1). Every Claude call is a fake."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
from _helpers import CLASSIFY_JSON, parse_sse

from leetcoach import app as app_module
from leetcoach import claude_cli, sandbox

ROOT = Path(__file__).resolve().parent.parent
RUN = {"problem": "Two Sum\nExample: Input: 1\nOutput: 1", "mode": "learning",
       "language": "python"}


def _is_classify(prompt: str) -> bool:
    return "Classify the following" in prompt


def _authed():
    return claude_cli.AuthStatus(installed=True, logged_in=True)


class Recorder:
    def __init__(self, text="# Notes\n"):
        self.text = text
        self.calls: list[dict] = []

    def __call__(self, prompt, **kwargs):
        if _is_classify(prompt):
            return iter([json.dumps(CLASSIFY_JSON)])
        self.calls.append(kwargs)
        return iter([self.text])


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(out))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(out / "topic_index.json"))
    monkeypatch.delenv("LEETCOACH_MODEL", raising=False)
    return out


def _client(run_fn, auth_probe=_authed):
    application = app_module.create_app(run_fn=run_fn, auth_probe=auth_probe)
    application.config.update(TESTING=True)
    return application.test_client()


def _in_thread(fn):
    box: dict = {}
    t = threading.Thread(target=lambda: box.setdefault("v", fn()), daemon=True)
    t.start()
    return t, box


def _app_js() -> str:
    return (ROOT / "leetcoach" / "static" / "app.js").read_text(encoding="utf-8")


# --- the page's Stop: a bounded cancel request -----------------------------

def test_stop_cancel_request_has_a_short_timeout():
    """An unresponsive server must not leave Stop hanging: the cancel POST is
    aborted after ~3 s and the stream is then aborted as before."""
    js = _app_js()
    m = re.search(r"function cancelRun\(runId\) \{(.*?)\n  \}\n", js, re.DOTALL)
    assert m, "cancelRun not found"
    body = m.group(1)
    assert "new AbortController()" in body
    assert re.search(r"signal:\s*\w+\.signal", body)
    t = re.search(r"setTimeout\(.*?\},\s*(\w+)\)", body)
    assert t, "no timeout on the cancel request"
    arg = t.group(1)
    ms = int(arg) if arg.isdigit() else int(re.search(rf"\b{arg}\s*=\s*(\d+)", js).group(1))
    assert 1000 <= ms <= 5000
    assert "clearTimeout" in body


# --- M1: a cancel that arrives after the run committed to saving ---------------

def test_cancel_after_commit_is_refused_and_the_run_saves(env, monkeypatch):
    from leetcoach import storage

    saving = threading.Event()
    release = threading.Event()
    saved = []
    real_save = storage.save_learning

    def slow_save(*args, **kwargs):
        saving.set()
        release.wait(5)
        path = real_save(*args, **kwargs)
        saved.append(path)
        return path

    monkeypatch.setattr(storage, "save_learning", slow_save)
    c = _client(Recorder())
    t, box = _in_thread(
        lambda: c.post("/run", json={**RUN, "run_id": "c1"}).get_data(as_text=True))
    try:
        assert saving.wait(5)
        resp = c.post("/run/cancel", json={"run_id": "c1"})
        assert resp.status_code == 200
        assert resp.get_json() == {"cancelled": False}
    finally:
        release.set()
    t.join(10)
    events = parse_sse(box["v"])[1]
    assert events[-1][0] == "done"
    assert saved and Path(saved[0]).exists()


def test_cancel_before_commit_still_cancels(env, monkeypatch):
    """The commit point is the save: a cancel while Claude streams still wins."""
    gate = threading.Event()
    started = threading.Event()

    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            yield json.dumps(CLASSIFY_JSON)
            return
        yield "partial "
        started.set()
        gate.wait(5)
        yield "rest"

    c = _client(run_fn)
    t, box = _in_thread(
        lambda: c.post("/run", json={**RUN, "run_id": "c2"}).get_data(as_text=True))
    assert started.wait(5)
    resp = c.post("/run/cancel", json={"run_id": "c2"})
    assert resp.get_json() == {"cancelled": True}
    gate.set()
    t.join(10)
    assert parse_sse(box["v"])[1][-1][0] == "cancelled"  # SP5 fix B2
    assert not (env / "learning").exists()


def test_client_does_not_show_stopped_when_the_run_already_committed():
    # SP5 fix R8: the decision lives in core.cancelOutcome (node-tested in
    # tests/js/core.test.js); Stop must still route through it.
    js = _app_js()
    assert 'core.cancelOutcome(r.status, d) === "committed"' in js
    core_js = (ROOT / "leetcoach" / "static" / "lib" / "core.js").read_text(encoding="utf-8")
    assert "body.cancelled === false" in core_js


# --- M2: verification is cancellable -------------------------------------------

def test_cancel_during_verification_stops_the_sandbox(env, monkeypatch):
    seen = {}
    verifying = threading.Event()

    def cancellable_verify(code, problem, language, *, cancel=None):
        seen["cancel"] = cancel
        verifying.set()
        stopped = cancel.wait(5) if cancel is not None else False
        seen["stopped"] = stopped
        return sandbox.VerifyResult(status="not_verified", note="verification cancelled")

    monkeypatch.setattr(sandbox, "verify_answer", cancellable_verify)
    rec = Recorder("Answer.\n\n```python solution\nprint(1)\n```\n")
    c = _client(rec)
    t, box = _in_thread(lambda: c.post(
        "/run", json={**RUN, "mode": "answer", "tier": "normal", "run_id": "v2"},
    ).get_data(as_text=True))
    assert verifying.wait(5)
    started = time.monotonic()
    assert c.post("/run/cancel", json={"run_id": "v2"}).get_json() == {"cancelled": True}
    t.join(10)
    assert seen["stopped"] is True  # the sandbox was told to stop ...
    assert time.monotonic() - started < 4  # ... and did not run out its clock
    name, msg = parse_sse(box["v"])[1][-1]
    assert name == "cancelled" and "cancelled" in msg.lower()
    assert not (env / "answers").exists()


# --- B2: a user Stop is reported as `cancelled`, not as an error ---------------

class CancellableRun:
    def __init__(self):
        self.cancelled = threading.Event()
        self.started = threading.Event()
        self._sent = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._sent:
            self._sent = True
            self.started.set()
            return "partial "
        if self.cancelled.wait(10):
            raise claude_cli.ClaudeCancelledError("cancelled")
        raise StopIteration

    def cancel(self):
        self.cancelled.set()


def test_cancelled_run_ends_with_a_cancelled_event_not_an_error(env):
    runs: list = []

    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            return iter([json.dumps(CLASSIFY_JSON)])
        r = CancellableRun()
        runs.append(r)
        return r

    c = _client(run_fn)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault(
        "body", c.post("/run", json={**RUN, "run_id": "s1"}).get_data(as_text=True)),
        daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (runs and runs[0].started.is_set()):
        time.sleep(0.02)
    assert c.post("/run/cancel", json={"run_id": "s1"}).get_json() == {"cancelled": True}
    t.join(10)
    events = parse_sse(out["body"])[1]
    assert "error" not in [name for name, _ in events]
    assert events[-1] == ("cancelled", "Run cancelled.")
    assert not (env / "learning").exists()


# --- M12: the cancel log names the call it was cancelling ----------------------

def test_failed_cancel_logs_the_study_call(env, caplog):
    started = threading.Event()
    stop = threading.Event()

    class BadCancelRun:
        def __iter__(self):
            yield "partial "
            started.set()
            stop.wait(5)
            raise claude_cli.ClaudeCancelledError("cancelled")

        def cancel(self):
            stop.set()
            raise RuntimeError("kill failed")

    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            return iter([json.dumps(CLASSIFY_JSON)])
        return BadCancelRun()

    c = _client(run_fn)
    t, _box = _in_thread(
        lambda: c.post("/run", json={**RUN, "run_id": "m12"}).get_data(as_text=True))
    assert started.wait(5)
    with caplog.at_level("ERROR"):
        c.post("/run/cancel", json={"run_id": "m12"})
    t.join(10)
    assert "could not cancel the study call" in caplog.text
    assert "classifier call" not in caplog.text


# --- /ask/cancel (B20) ---------------------------------------------------------

class BlockingCall:
    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def __iter__(self):
        self.started.set()
        if self.cancelled.wait(5):
            raise claude_cli.ClaudeCancelledError("cancelled")
        yield "late"

    def cancel(self):
        self.cancelled.set()


def test_ask_cancel_kills_the_call(env):
    call = BlockingCall()
    client = _client(lambda prompt, **kw: call)
    box = {}
    t = threading.Thread(target=lambda: box.setdefault(
        "resp", client.post("/ask", json={"question": "heap?", "ask_id": "q1"})), daemon=True)
    t.start()
    assert call.started.wait(5)
    resp = client.post("/ask/cancel", json={"ask_id": "q1"})
    assert resp.status_code == 200 and resp.get_json() == {"cancelled": True}
    t.join(5)
    assert box["resp"].status_code == 409
    assert box["resp"].get_json()["error"] == "Quick Ask cancelled."
    # The id is released once the ask is over.
    assert client.post("/ask/cancel", json={"ask_id": "q1"}).status_code == 404


def test_ask_cancel_validation(env):
    client = _client(Recorder())
    assert client.post("/ask/cancel", json={"ask_id": "bad id!"}).status_code == 400
    assert client.post("/ask/cancel", json={}).status_code == 400
    assert client.post("/ask/cancel", json={"ask_id": "nope"}).status_code == 404
    assert client.post("/ask", json={"question": "q", "ask_id": "bad id!"}).status_code == 400
    assert client.post("/ask", json={"question": "q", "ask_id": 5}).status_code == 400


def test_ask_with_an_id_still_answers(env):
    client = _client(lambda prompt, **kw: iter(["Use heapq."]))
    resp = client.post("/ask", json={"question": "heap?", "ask_id": "q2"})
    assert resp.status_code == 200 and resp.get_json() == {"answer": "Use heapq."}


# --- R1: a Quick Ask cancelled before run_fn returned is still cancelled -------

def test_quick_ask_cancel_that_races_ahead_of_run_fn(env):
    entered = threading.Event()
    release = threading.Event()

    class Call:
        def __init__(self):
            self.cancel_calls = 0
            self.iterated = False

        def __iter__(self):
            self.iterated = True
            return iter(["too late"])

        def cancel(self):
            self.cancel_calls += 1

    call = Call()

    def run_fn(prompt, **kwargs):
        entered.set()
        release.wait(5)  # /ask/cancel lands while run_fn is still starting
        return call

    client = _client(run_fn)
    box = {}
    t = threading.Thread(target=lambda: box.setdefault(
        "resp", client.post("/ask", json={"question": "heap?", "ask_id": "early"})),
        daemon=True)
    t.start()
    assert entered.wait(5)
    resp = client.post("/ask/cancel", json={"ask_id": "early"})
    assert resp.status_code == 200 and resp.get_json() == {"cancelled": True}
    release.set()
    t.join(5)
    assert box["resp"].status_code == 409
    assert call.cancel_calls == 1
    assert not call.iterated
    assert client.post("/ask/cancel", json={"ask_id": "early"}).status_code == 404
