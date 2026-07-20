"""Classifier off the critical path + cheap-model knob (audit6 P2-4).

Three properties of the `/run` endpoint's classification step:

1. **Concurrency:** classification runs on a background thread, so the FIRST
   answer delta reaches the SSE stream while the classifier call is still
   pending (previously a full classify round-trip completed before any answer
   text streamed). Proven deterministically with ``threading.Event``s: the fake
   classifier call BLOCKS until the answer stream's first delta has been
   yielded — the old sequential code would time out here, the new code streams
   straight through and joins the thread before saving.

2. **Cheap model:** the classifier call receives ``model=<LEETCOACH_CLASSIFIER_MODEL>``
   (default ``haiku``) while the answer call keeps the default model (no
   ``model=`` kwarg).

3. **Degradation:** if the classify path raises inside the background thread,
   the run still completes and saves under ``problem_type == "uncategorized"``.
"""
from __future__ import annotations

import json
import threading

from _helpers import CLASSIFY_JSON
from _helpers import parse_sse as _parse_sse

import app as app_module
import config

ANSWER_MARKDOWN = (
    "Reasoning first.\n\n"
    "```python\n"
    "def two_sum(nums, target):\n"
    "    seen = {}\n"
    "    for i, n in enumerate(nums):\n"
    "        if target - n in seen:\n"
    "            return [seen[target - n], i]\n"
    "        seen[n] = i\n"
    "```\n"
)

RUN_PAYLOAD = {
    "problem": "Two Sum: return indices of two numbers adding to target.",
    "mode": "answer",
    "language": "python",
    "tier": "normal",
}


def _is_classify(prompt: str) -> bool:
    return "Classify the following" in prompt and "Respond with ONLY a tiny" in prompt


def _make_client(tmp_path, monkeypatch, run_fn):
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))
    application = app_module.create_app(run_fn=run_fn)
    application.config.update(TESTING=True)
    return application.test_client()


# --- 1) classification no longer blocks the first answer delta ------------

def test_first_delta_streams_while_classification_pending(tmp_path, monkeypatch):
    """The fake classifier BLOCKS until the answer stream has yielded its first
    delta. Sequential code (classify-then-stream) deadlocks/times out here;
    concurrent code streams the delta, releases the classifier, and the final
    done payload still carries the classifier's real result (join happened)."""
    release_classify = threading.Event()
    classify_finished = threading.Event()
    classify_pending_at_first_delta = []

    def fake_run(prompt, **kwargs):
        if _is_classify(prompt):
            # Block until the answer stream produced its first delta. The
            # timeout only bounds a FAILING (still-sequential) run; the happy
            # path releases it immediately after the first delta.
            released = release_classify.wait(timeout=10)
            if not released:
                raise AssertionError(
                    "classifier ran on the critical path: it was never "
                    "released by the answer stream's first delta"
                )
            classify_finished.set()
            yield json.dumps(CLASSIFY_JSON)
        else:
            yield "First delta of the answer. "
            # We only resume here after the delta above was consumed by the
            # SSE plumbing — i.e. it has streamed. Classification must still
            # be pending at this point.
            classify_pending_at_first_delta.append(not classify_finished.is_set())
            release_classify.set()
            yield ANSWER_MARKDOWN

    c = _make_client(tmp_path, monkeypatch, fake_run)
    resp = c.post("/run", json=RUN_PAYLOAD)
    assert resp.status_code == 200
    text_chunks, events = _parse_sse(resp.get_data(as_text=True))

    assert classify_pending_at_first_delta == [True], (
        "classification finished before the first answer delta streamed — "
        "it is still on the critical path"
    )
    assert "def two_sum" in "".join(text_chunks)

    done = [p for (name, p) in events if name == "done"]
    assert done, f"no done event in: {events}"
    # the join before saving picked up the classifier's REAL result
    assert done[0]["problem_type"] == "two_pointers"
    assert done[0]["topics"] == ["arrays"]


# --- 2) cheap-model knob ---------------------------------------------------

def test_classifier_call_uses_cheap_model_answer_call_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("LEETCOACH_CLASSIFIER_MODEL", "cheap-model-x")
    calls = []

    def fake_run(prompt, **kwargs):
        calls.append({"classify": _is_classify(prompt), "kwargs": kwargs})
        yield json.dumps(CLASSIFY_JSON) if _is_classify(prompt) else ANSWER_MARKDOWN

    c = _make_client(tmp_path, monkeypatch, fake_run)
    resp = c.post("/run", json=RUN_PAYLOAD)
    assert resp.status_code == 200
    _, events = _parse_sse(resp.get_data(as_text=True))
    assert [n for (n, _) in events if n == "done"]

    classify_calls = [c_ for c_ in calls if c_["classify"]]
    answer_calls = [c_ for c_ in calls if not c_["classify"]]
    assert len(classify_calls) == 1 and len(answer_calls) == 1
    assert classify_calls[0]["kwargs"].get("model") == "cheap-model-x"
    # the answer call keeps the default model (no explicit model= override)
    assert "model" not in answer_calls[0]["kwargs"]


def test_classifier_model_knob_defaults_to_haiku(monkeypatch):
    monkeypatch.delenv("LEETCOACH_CLASSIFIER_MODEL", raising=False)
    assert config.classifier_model() == "haiku"
    monkeypatch.setenv("LEETCOACH_CLASSIFIER_MODEL", "sonnet")
    assert config.classifier_model() == "sonnet"


# --- 3) a classify crash inside the thread degrades to uncategorized -------

def test_classify_raising_in_thread_degrades_to_uncategorized(tmp_path, monkeypatch):
    """classifier.classify never raises by contract, but even if it somehow
    does, the background thread must not take the run down: the run completes
    and saves under the fallback ``uncategorized`` type."""

    def exploding_classify(problem, **kwargs):
        raise RuntimeError("classifier blew up")

    monkeypatch.setattr(app_module.classifier, "classify", exploding_classify)

    def fake_run(prompt, **kwargs):
        yield ANSWER_MARKDOWN

    c = _make_client(tmp_path, monkeypatch, fake_run)
    resp = c.post("/run", json=RUN_PAYLOAD)
    assert resp.status_code == 200
    _, events = _parse_sse(resp.get_data(as_text=True))

    assert not [n for (n, _) in events if n == "error"]
    done = [p for (name, p) in events if name == "done"]
    assert done, f"no done event in: {events}"
    assert done[0]["problem_type"] == "uncategorized"
    assert done[0]["topics"] == []
    # files were still written, under the fallback bucket
    saved = [p for p in (tmp_path / "answers").rglob("*") if p.is_file()]
    assert saved and all("uncategorized" in str(p) for p in saved)
