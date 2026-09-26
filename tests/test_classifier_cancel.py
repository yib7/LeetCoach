"""A6 (app side): the background classifier can never wedge or outlive a run.

* The ``/run`` stream used to ``join()`` the classifier thread with no
  timeout; a classifier call that never returns hung the save forever. The
  join is now bounded (``CLASSIFIER_JOIN_TIMEOUT``); on expiry the classifier
  call is cancelled and the run saves under the fallback type.
* When the SSE client disconnects (or the answer stream fails) the classifier
  call is cancelled too, instead of burning a `claude` process nobody needs.

The fake classifier call mimics :class:`claude_cli.ClaudeRun`: an iterator
that blocks until its ``cancel()`` is called.
"""
from __future__ import annotations

import threading
import time

from _helpers import parse_sse

import app as app_module
import claude_cli

RUN_PAYLOAD = {
    "problem": "Two Sum: return indices of two numbers adding to target.",
    "mode": "learning",
    "language": "python",
}


def _is_classify(prompt: str) -> bool:
    return "Classify the following" in prompt


class BlockingRun:
    """A classifier call that blocks until cancelled (or a safety timeout)."""

    def __init__(self):
        self.cancel_called = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        if not self.cancel_called.wait(30):
            raise StopIteration
        raise claude_cli.ClaudeCancelledError("cancelled")

    def cancel(self):
        self.cancel_called.set()


def _make(tmp_path, monkeypatch, answer_run):
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(tmp_path / "topics.json"))
    cls_run = BlockingRun()

    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            return cls_run
        return answer_run(prompt)

    application = app_module.create_app(run_fn=run_fn)
    application.config.update(TESTING=True)
    return application.test_client(), cls_run


def test_classifier_join_is_bounded_and_the_call_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "CLASSIFIER_JOIN_TIMEOUT", 0.5)

    def answer(prompt):
        yield "# Notes\n\nA hash map remembers seen values.\n"

    client, cls_run = _make(tmp_path, monkeypatch, answer)
    out: dict = {}

    def go():
        out["body"] = client.post("/run", json=RUN_PAYLOAD).get_data(as_text=True)

    try:
        start = time.monotonic()
        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(15)
        assert not t.is_alive(), "the run waited on the classifier forever"
        assert time.monotonic() - start < 10
    finally:
        cls_run.cancel_called.set()  # never leave the fake blocked
    _, events = parse_sse(out["body"])
    name, payload = events[-1]
    assert name == "done"
    assert payload["problem_type"] == "uncategorized"
    assert cls_run.cancel_called.is_set()


def test_client_disconnect_cancels_the_classifier_call(tmp_path, monkeypatch):
    release = threading.Event()

    def answer(prompt):
        yield "first chunk"
        release.wait(30)  # the answer is still streaming when the client leaves
        yield "never delivered"

    client, cls_run = _make(tmp_path, monkeypatch, answer)
    try:
        resp = client.post("/run", json=RUN_PAYLOAD, buffered=False)
        chunks = iter(resp.response)
        first = next(chunks)
        assert b"first chunk" in first
        release.set()
        resp.close()  # the browser went away: Flask closes the generator
        assert cls_run.cancel_called.wait(5), "classifier call left running after disconnect"
    finally:
        release.set()
        cls_run.cancel_called.set()


def test_answer_failure_cancels_the_classifier_call(tmp_path, monkeypatch):
    def answer(prompt):
        raise claude_cli.ClaudeUnavailableError("boom")
        yield  # pragma: no cover

    client, cls_run = _make(tmp_path, monkeypatch, answer)
    try:
        body = client.post("/run", json=RUN_PAYLOAD).get_data(as_text=True)
        _, events = parse_sse(body)
        assert events[-1][0] == "error"
        assert cls_run.cancel_called.wait(5)
    finally:
        cls_run.cancel_called.set()


def test_fast_classifier_is_not_cancelled_or_delayed(tmp_path, monkeypatch):
    # Regression guard: a normal classifier result is still used.
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(tmp_path / "topics.json"))

    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            yield '{"problem_type": "sliding_window", "topics": ["deque"]}'
        else:
            yield "# Notes\n"

    application = app_module.create_app(run_fn=run_fn)
    body = application.test_client().post("/run", json=RUN_PAYLOAD).get_data(as_text=True)
    _, events = parse_sse(body)
    assert events[-1][0] == "done"
    assert events[-1][1]["problem_type"] == "sliding_window"
