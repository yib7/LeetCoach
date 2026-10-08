"""SP4 review fixes in the web layer: a pinned model id survives the per-run
picker alias (I1), cancel vs. save race (M1), cancellable verification (M2),
single-instance probing across the fallback span (M10), the same-site auth
probe (M11) and the cancel log message (M12). Every Claude call is a fake."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
from _helpers import CLASSIFY_JSON, parse_sse

import app as app_module
import claude_cli
import sandbox

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
    return (ROOT / "static" / "app.js").read_text(encoding="utf-8")


# --- I1: a pinned model id is not overridden by its own picker alias -----------

def test_posting_the_configured_alias_runs_the_pinned_id(env, monkeypatch):
    monkeypatch.setenv("LEETCOACH_MODEL", "claude-sonnet-4-5")
    rec = Recorder()
    body = _client(rec).post("/run", json={**RUN, "model": "sonnet"}).get_data(as_text=True)
    assert parse_sse(body)[1][-1][0] == "done"
    assert rec.calls[0]["model"] == "claude-sonnet-4-5"


def test_posting_a_different_alias_still_switches_model(env, monkeypatch):
    monkeypatch.setenv("LEETCOACH_MODEL", "claude-sonnet-4-5")
    rec = Recorder()
    _client(rec).post("/run", json={**RUN, "model": "haiku"}).get_data(as_text=True)
    assert rec.calls[0]["model"] == "haiku"


def test_client_sends_model_only_after_the_picker_changed():
    js = _app_js()
    # the picker click marks the choice as this tab's own ...
    assert re.search(r'group === "model"\)\s*\{[^}]*modelTouched = true', js)
    # ... and only then does a run carry an explicit model
    assert re.search(r"if \(modelTouched && model\) body\.model = model;", js)


# --- M1: a cancel that arrives after the run committed to saving ---------------

def test_cancel_after_commit_is_refused_and_the_run_saves(env, monkeypatch):
    import storage

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
    assert parse_sse(box["v"])[1][-1][0] == "error"
    assert not (env / "learning").exists()


def test_client_does_not_show_stopped_when_the_run_already_committed():
    js = _app_js()
    assert "cancelled === false" in js


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
    assert name == "error" and "cancelled" in msg.lower()
    assert not (env / "answers").exists()


def test_abandoned_blocking_call_signals_its_cancel(monkeypatch):
    monkeypatch.setattr(app_module, "SSE_PING_INTERVAL", 0.01)
    release = threading.Event()
    abandoned = threading.Event()

    def work():
        release.wait(5)
        return 1

    gen = app_module._call_with_heartbeat(work, on_abandon=abandoned.set)
    assert next(gen) == app_module.SSE_PING
    gen.close()  # the client went away mid-verification
    assert abandoned.is_set()
    release.set()


def test_finished_blocking_call_does_not_signal_cancel(monkeypatch):
    abandoned = threading.Event()
    gen = app_module._call_with_heartbeat(lambda: 7, on_abandon=abandoned.set)
    with pytest.raises(StopIteration) as stop:
        while True:
            next(gen)
    assert stop.value.value == 7
    assert not abandoned.is_set()


def test_verify_python_stops_promptly_when_cancelled():
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    r = sandbox.verify_python("while True:\n    pass\n", "", "x", timeout=30, cancel=cancel)
    assert time.monotonic() - started < 10
    assert r.status == "not_verified"
    assert "cancel" in r.note


def test_verify_python_does_not_start_when_already_cancelled(monkeypatch):
    cancel = threading.Event()
    cancel.set()

    def no_spawn(*a, **k):
        raise AssertionError("must not spawn a cancelled verification")

    monkeypatch.setattr(sandbox.subprocess, "Popen", no_spawn)
    r = sandbox.verify_python("print(1)\n", "", "1", cancel=cancel)
    assert r.status == "not_verified" and "cancel" in r.note


def test_samples_stop_after_a_cancel(monkeypatch):
    cancel = threading.Event()
    calls = []

    def fake_verify(code, stdin_text, expected_stdout, **kwargs):
        calls.append(kwargs.get("cancel"))
        cancel.set()  # cancelled while the first sample ran
        return sandbox.VerifyResult(status="not_verified", note="verification cancelled")

    monkeypatch.setattr(sandbox, "verify_python", fake_verify)
    samples = [sandbox.Sample(stdin="", expected_stdout="1")] * 3
    r = sandbox._verify_python_samples("print(1)\n", samples, cancel=cancel)
    assert calls == [cancel]
    assert r.status == "not_verified"


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
    url = app_module._find_existing_instance(
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
    url = app_module._find_existing_instance(
        "127.0.0.1", 5000, span=3, opener=_fake_opener({}, seen),
        is_free=lambda host, port: False,
    )
    assert url is None
    assert len(seen) == 4


def test_probing_is_bounded_by_an_overall_budget():
    clock = [0.0]
    seen: list = []
    url = app_module._find_existing_instance(
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
        assert app_module._port_is_free("127.0.0.1", srv.server_port) is False
    finally:
        srv.server_close()
    assert app_module._port_is_free("127.0.0.1", srv.server_port) is True


def test_main_uses_the_span_probe(monkeypatch):
    opened = []
    monkeypatch.setattr(app_module, "_find_existing_instance",
                        lambda host, port, **kw: "http://127.0.0.1:5003/")

    def no_choose(*a, **k):
        raise AssertionError("must not fall back to another port")

    monkeypatch.setattr(app_module, "_choose_port", no_choose)
    rc = app_module.main(open_browser=opened.append, serve=lambda port: None)
    assert rc == 0
    assert opened == ["http://127.0.0.1:5003/"]


# --- M11: same-site loads may not trigger the auth probe ------------------------

def test_index_refuses_same_site_subresource_loads():
    probes = []

    def probe():
        probes.append(1)
        return _authed()

    c = _client(Recorder(), auth_probe=probe)
    resp = c.get("/", headers={"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors",
                               "Sec-Fetch-Dest": "image"})
    assert resp.status_code == 403
    assert probes == []
    for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"},
                    {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate",
                     "Sec-Fetch-Dest": "document"}):
        assert c.get("/", headers=headers).status_code == 200, headers


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
    t, box = _in_thread(
        lambda: c.post("/run", json={**RUN, "run_id": "m12"}).get_data(as_text=True))
    assert started.wait(5)
    with caplog.at_level("ERROR"):
        c.post("/run/cancel", json={"run_id": "m12"})
    t.join(10)
    assert "could not cancel the study call" in caplog.text
    assert "classifier call" not in caplog.text
