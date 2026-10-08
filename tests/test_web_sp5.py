"""SP5 server additions for the front end: the concrete model id from the
stream-json ``system/init`` event (model chip), SSE ``phase`` events
(streaming -> verifying i/n -> saving), the library ``verdict`` field (B18) and
Quick Ask cancellation (B20). Every Claude call is a fake."""
from __future__ import annotations

import json
import queue
import threading
import time

import pytest
from _helpers import CLASSIFY_JSON, parse_sse

import app as app_module
import claude_cli
import sandbox

RUN = {"problem": "Two Sum\nExample: Input: 1\nOutput: 1", "mode": "learning",
       "language": "python"}
ANSWER = {**RUN, "mode": "answer", "tier": "normal"}
DOC = "Answer.\n\n```python solution\nprint(input())\n```\n"


def _is_classify(prompt: str) -> bool:
    return "Classify the following" in prompt


def _authed():
    return claude_cli.AuthStatus(installed=True, logged_in=True)


class ModelCall:
    """A run_fn result shaped like claude_cli.ClaudeRun: iterable + .model."""

    def __init__(self, chunks, model=None):
        self._it = iter(chunks)
        self.model = model

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)


def _run_fn(text=DOC, model="claude-opus-5-5"):
    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            return iter([json.dumps(CLASSIFY_JSON)])
        return ModelCall([text[:5], text[5:]], model=model)
    return run_fn


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(out))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(out / "topic_index.json"))
    return out


def _client(run_fn):
    application = app_module.create_app(run_fn=run_fn, auth_probe=_authed)
    application.config.update(TESTING=True)
    return application.test_client()


# --- claude_cli: the concrete model id ----------------------------------------

def _lines(*objs):
    return [json.dumps(o) for o in objs]


def test_parser_captures_model_from_system_init():
    handle = claude_cli.ClaudeRun()
    lines = _lines(
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-opus-5-5"},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "hi"}}},
        {"type": "assistant", "message": {"model": "other", "content": []}},
        {"type": "result", "subtype": "success", "result": "hi"},
    )
    assert list(claude_cli._iter_text_deltas(lines, handle)) == ["hi"]
    assert handle.model == "claude-opus-5-5"
    assert handle.session_id == "s1"


def test_parser_falls_back_to_the_assistant_message_model():
    handle = claude_cli.ClaudeRun()
    lines = _lines(
        {"type": "system", "subtype": "init", "model": None},
        {"type": "assistant", "message": {"model": "claude-sonnet-5-5",
                                          "content": [{"type": "text", "text": "x"}]}},
        {"type": "result", "subtype": "success", "result": "x"},
    )
    assert "".join(claude_cli._iter_text_deltas(lines, handle)) == "x"
    assert handle.model == "claude-sonnet-5-5"


def test_parser_ignores_odd_model_shapes():
    handle = claude_cli.ClaudeRun()
    lines = _lines(
        {"type": "system", "subtype": "init", "model": ["x"]},
        {"type": "system", "subtype": "other", "model": "nope"},
        {"type": "result", "subtype": "success", "result": "y"},
    )
    assert "".join(claude_cli._iter_text_deltas(lines, handle)) == "y"
    assert handle.model is None


# --- /run: meta + phase events --------------------------------------------------

def test_run_sends_the_concrete_model_and_phases(env):
    body = _client(_run_fn(text="# Notes\n")).post("/run", json=RUN).get_data(as_text=True)
    text, events = parse_sse(body)
    assert "".join(text) == "# Notes\n"
    names = [name for name, _ in events]
    assert names[0] == "phase" and events[0][1] == {"phase": "streaming"}
    assert ("meta", {"model": "claude-opus-5-5"}) in events
    assert names.count("meta") == 1
    assert ("phase", {"phase": "saving"}) in events
    assert names[-1] == "done"
    assert events[-1][1]["model"] == "claude-opus-5-5"
    # The meta frame precedes the first text delta in the raw stream.
    assert body.index("event: meta") < body.index('data: "# Not')


def test_run_without_a_reported_model_sends_no_meta(env):
    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            return iter([json.dumps(CLASSIFY_JSON)])
        return iter(["# Notes\n"])

    events = parse_sse(_client(run_fn).post("/run", json=RUN).get_data(as_text=True))[1]
    assert "meta" not in [name for name, _ in events]
    assert "model" not in events[-1][1]


def test_answer_run_reports_verification_progress(env, monkeypatch):
    def fake_verify(code, problem, language, *, cancel=None, progress=None):
        for i in (1, 2, 3):
            progress(i, 3)
        return sandbox.VerifyResult(status="pass", note="all 3 sample(s) passed")

    monkeypatch.setattr(sandbox, "verify_answer", fake_verify)
    events = parse_sse(_client(_run_fn()).post("/run", json=ANSWER).get_data(as_text=True))[1]
    phases = [data for name, data in events if name == "phase"]
    assert phases[0] == {"phase": "streaming"}
    assert {"phase": "verifying"} in phases
    steps = [p for p in phases if p.get("i")]
    assert steps == [{"phase": "verifying", "i": i, "n": 3} for i in (1, 2, 3)]
    assert phases[-1] == {"phase": "saving"}
    assert events[-1][0] == "done"
    assert events[-1][1]["verification"].startswith("✓")


def test_verifier_without_progress_kwarg_still_works(env, monkeypatch):
    def old_verify(code, problem, language, *, cancel=None):
        return sandbox.VerifyResult(status="fail", note="0/1 sample(s) passed")

    monkeypatch.setattr(sandbox, "verify_answer", old_verify)
    events = parse_sse(_client(_run_fn()).post("/run", json=ANSWER).get_data(as_text=True))[1]
    assert events[-1][0] == "done"
    assert "FAIL" in events[-1][1]["verification"]


def test_verify_python_samples_calls_progress(monkeypatch):
    seen = []
    monkeypatch.setattr(
        sandbox, "verify_python",
        lambda *a, **k: sandbox.VerifyResult(status="pass", note=""),
    )
    samples = [sandbox.Sample("1", "1"), sandbox.Sample("2", "2")]
    result = sandbox._verify_python_samples(
        "print(1)", samples, progress=lambda i, n: seen.append((i, n)))
    assert result.status == "pass"
    assert seen == [(1, 2), (2, 2)]


def test_a_failing_progress_callback_is_ignored(monkeypatch):
    monkeypatch.setattr(
        sandbox, "verify_python",
        lambda *a, **k: sandbox.VerifyResult(status="pass", note=""),
    )

    def boom(i, n):
        raise RuntimeError("display only")

    result = sandbox._verify_python_samples("x", [sandbox.Sample("1", "1")], progress=boom)
    assert result.status == "pass"


def test_call_with_heartbeat_yields_updates_then_returns():
    updates: queue.Queue = queue.Queue()

    def work():
        updates.put("frame-1")
        time.sleep(0.05)
        updates.put("frame-2")
        return 42

    gen = app_module._call_with_heartbeat(work, updates=updates)
    frames = []
    try:
        while True:
            frames.append(next(gen))
    except StopIteration as stop:
        assert stop.value == 42
    assert [f for f in frames if f.startswith("frame")] == ["frame-1", "frame-2"]


# --- /library: verdict (B18) -------------------------------------------------

@pytest.mark.parametrize("line, verdict", [
    ("✓ Sample tests PASS (all 2 sample(s) passed)", "pass"),
    ("✗ Sample tests FAIL (1/3 sample(s) passed, 1 errored)", "fail"),
    ("✗ Sample tests ERROR (code errored on 2/2 sample(s))", "error"),
    ("⚠ not auto-verified (no sample I/O found in problem)", "not_verified"),
    ("⚠ not auto-verified (verifier error: Sample tests FAILED oddly)", "not_verified"),
])
def test_verdict_from_text(line, verdict):
    doc = f"# T\n\n---\n\n**Verification:** {line}\n\n**Failed samples:**\n"
    assert app_module.verdict_from_text(doc) == verdict


def test_verdict_from_text_uses_the_last_line_and_none_without_one():
    assert app_module.verdict_from_text("# Learning notes only\n") is None
    doc = ("**Verification:** ✗ Sample tests FAIL (0/1)\n\n## Follow-up\n\n"
           "**Verification:** ✓ Sample tests PASS\n")
    assert app_module.verdict_from_text(doc) == "pass"


def test_library_listing_carries_the_verdict(env):
    (env / "answers" / "hash_map").mkdir(parents=True)
    (env / "learning" / "hash_map_learning").mkdir(parents=True)
    (env / "answers" / "hash_map" / "two_sum__normal.md").write_text(
        "# Two Sum\n\n---\n\n**Verification:** ✗ Sample tests FAIL (0/1 sample(s) passed)\n",
        encoding="utf-8")
    (env / "answers" / "hash_map" / "two_sum__normal.py").write_text("print(1)\n")
    (env / "learning" / "hash_map_learning" / "two_sum.md").write_text("# Notes\n")
    files = {f["path"]: f for f in _client(_run_fn()).get("/library").get_json()["files"]}
    assert files["answers/hash_map/two_sum__normal.md"]["verdict"] == "fail"
    assert "verdict" not in files["answers/hash_map/two_sum__normal.py"]
    assert "verdict" not in files["learning/hash_map_learning/two_sum.md"]


def test_saved_answer_shows_up_with_its_verdict(env, monkeypatch):
    monkeypatch.setattr(
        sandbox, "verify_answer",
        lambda code, problem, language, *, cancel=None, progress=None:
            sandbox.VerifyResult(status="pass", note="all 1 sample(s) passed"),
    )
    client = _client(_run_fn())
    assert parse_sse(client.post("/run", json=ANSWER).get_data(as_text=True))[1][-1][0] == "done"
    mds = [f for f in client.get("/library").get_json()["files"] if f["path"].endswith(".md")]
    assert [f["verdict"] for f in mds] == ["pass"]


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
    client = _client(_run_fn())
    assert client.post("/ask/cancel", json={"ask_id": "bad id!"}).status_code == 400
    assert client.post("/ask/cancel", json={}).status_code == 400
    assert client.post("/ask/cancel", json={"ask_id": "nope"}).status_code == 404
    assert client.post("/ask", json={"question": "q", "ask_id": "bad id!"}).status_code == 400
    assert client.post("/ask", json={"question": "q", "ask_id": 5}).status_code == 400


def test_ask_with_an_id_still_answers(env):
    client = _client(lambda prompt, **kw: iter(["Use heapq."]))
    resp = client.post("/ask", json={"question": "heap?", "ask_id": "q2"})
    assert resp.status_code == 200 and resp.get_json() == {"answer": "Use heapq."}
