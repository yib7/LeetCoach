"""SP5 fix round: the orchestrator's browser pass (B1-B5) and the code-review
minors (R1-R7) that are testable server-side. Every Claude call is a fake; the
only real subprocess is this Python interpreter standing in for `claude`."""
from __future__ import annotations

import json
import sys
import threading
import time

import pytest
from _helpers import CLASSIFY_JSON, parse_sse

import app as app_module
import claude_cli

RUN = {"problem": "Two Sum\nExample: Input: 1\nOutput: 1", "mode": "learning",
       "language": "python"}


def _is_classify(prompt: str) -> bool:
    return "Classify the following" in prompt


def _authed():
    return claude_cli.AuthStatus(installed=True, logged_in=True)


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


def _delta(text):
    return json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})


def _result(text=""):
    return json.dumps({"type": "result", "subtype": "success", "result": text})


# --- B1: the prompt reaches the child byte-for-byte (no "\r\n") ---------------

def test_real_runner_feeds_stdin_without_newline_translation():
    script = "import sys\nsys.stdout.write(repr(sys.stdin.buffer.read()) + '\\n')\n"
    prompt = "line one\nFAKE_CUT\n\nlast — ünïcode\n"
    out: dict = {}

    def go():
        try:
            out["lines"] = list(claude_cli._real_runner([sys.executable, "-c", script], prompt))
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            out["exc"] = exc

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(20)
    assert not t.is_alive()
    if "exc" in out:
        raise out["exc"]
    received = eval(out["lines"][0].strip())  # noqa: S307 - our own repr() of bytes
    assert received == prompt.encode("utf-8")
    assert b"\r\n" not in received


def test_real_runner_broken_stdin_still_reports_the_child_failure():
    # The child exits at once without reading stdin: the binary feed must still
    # swallow the broken pipe and surface the nonzero exit (P2-3).
    script = "import sys\nsys.stderr.write('boom at startup\\n')\nsys.exit(3)\n"
    with pytest.raises(claude_cli.ClaudeUnavailableError, match="boom at startup"):
        list(claude_cli._real_runner([sys.executable, "-c", script], "x" * (4 * 1024 * 1024)))


# --- B3: no terminal `result` event -> incomplete, never a success -------------

def test_stream_without_result_event_raises_incomplete():
    with pytest.raises(claude_cli.ClaudeUnavailableError, match="no final result"):
        list(claude_cli._iter_text_deltas([_delta("half an ans")]))


def test_stream_with_result_event_is_a_success():
    assert list(claude_cli._iter_text_deltas([_delta("a"), _result("a")])) == ["a"]


def _cli_run_fn(*, cut_study=True, cut_classify=False, cut_ask=False):
    """A run_fn that goes through the real claude_cli parser with a fake runner,
    so the missing-`result` rule is exercised end to end."""

    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            lines = [_delta(json.dumps(CLASSIFY_JSON))]
            cut = cut_classify
        elif kwargs.get("persist_session") is False:  # Quick Ask
            lines = [_delta("Use heapq.")]
            cut = cut_ask
        else:
            lines = [_delta("# Notes\n"), _delta("half of the doc")]
            cut = cut_study
        if not cut:
            lines.append(_result())

        def runner(argv, stdin_text, **kw):
            yield from lines

        return claude_cli.run(prompt, runner=runner, **kwargs)

    return run_fn


def test_study_run_cut_short_is_an_error_and_saves_nothing(env):
    body = _client(_cli_run_fn()).post("/run", json=RUN).get_data(as_text=True)
    name, msg = parse_sse(body)[1][-1]
    assert name == "error"
    assert "no final result" in msg and "nothing was saved" in msg
    assert not (env / "learning").exists()


def test_study_run_with_result_still_saves(env):
    body = _client(_cli_run_fn(cut_study=False)).post("/run", json=RUN).get_data(as_text=True)
    assert parse_sse(body)[1][-1][0] == "done"
    assert (env / "learning").exists()


def test_classifier_cut_short_falls_back_and_the_run_still_saves(env):
    client = _client(_cli_run_fn(cut_study=False, cut_classify=True))
    events = parse_sse(client.post("/run", json=RUN).get_data(as_text=True))[1]
    assert events[-1][0] == "done"
    assert events[-1][1]["problem_type"] == "uncategorized"


def test_quick_ask_cut_short_is_a_502(env):
    resp = _client(_cli_run_fn(cut_ask=True)).post("/ask", json={"question": "heap?"})
    assert resp.status_code == 502
    assert "no final result" in resp.get_json()["error"]


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


# --- R2: only the app-written Verification block counts ------------------------

def test_model_written_verification_line_is_not_a_verdict():
    doc = ("# Learning notes\n\nWhen you are done, write:\n\n"
           "**Verification:** ✓ Sample tests PASS\n\nMore notes.\n")
    assert app_module.verdict_from_text(doc) is None


def test_follow_up_sections_are_ignored_for_the_verdict():
    doc = ("# T\n\nbody\n\n---\n\n**Verification:** ✗ Sample tests FAIL (0/1)\n\n"
           "## Follow-up\n\n---\n\n**Verification:** ✓ Sample tests PASS\n")
    assert app_module.verdict_from_text(doc) == "fail"


def test_a_model_follow_up_heading_before_the_verdict_does_not_hide_it():
    doc = ("# T\n\n## Follow-up\n\nask more\n\n---\n\n"
           "**Verification:** ✓ Sample tests PASS (1/1)\n")
    assert app_module.verdict_from_text(doc) == "pass"


def test_crlf_saved_doc_still_has_its_verdict():
    doc = "# T\r\n\r\nbody\r\n\r\n---\r\n\r\n**Verification:** ✓ Sample tests PASS\r\n"
    assert app_module.verdict_from_text(doc) == "pass"


# --- R7: the verdict cache forgets files that left the library -----------------

def test_verdict_cache_prunes_paths_no_longer_listed(env):
    folder = env / "answers" / "hash_map"
    folder.mkdir(parents=True)
    doc = folder / "two_sum__normal.md"
    doc.write_text("# T\n\n---\n\n**Verification:** ✓ Sample tests PASS\n", encoding="utf-8")
    client = _client(_cli_run_fn())
    client.get("/library")
    assert str(doc) in app_module._verdict_cache
    doc.unlink()
    client.get("/library")
    assert str(doc) not in app_module._verdict_cache
