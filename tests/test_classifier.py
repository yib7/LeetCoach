"""Tests for `classifier.py` — one short Claude call -> problem_type + topics.

The Claude call is injected via ``run_fn`` so no real `claude` is spawned. The
fake ``run_fn`` mirrors ``claude_cli.run``: it accepts ``(prompt, **kwargs)`` and
returns an iterable of text deltas. We assert:

* a clean JSON reply parses into a slug + topic list,
* the slug is itself slugified (defensive against a messy label from Claude),
* extra prose / markdown code fences around the JSON are tolerated,
* total garbage falls back to a sane slug (``uncategorized``) and empty topics,
* the problem text is actually passed into the prompt given to ``run_fn``.
"""
from __future__ import annotations

import json

import classifier


def make_run_fn(deltas):
    """Return (run_fn, calls); run_fn records its prompt and yields `deltas`."""
    calls = []

    def run_fn(prompt, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        for d in deltas:
            yield d

    return run_fn, calls


def json_deltas(obj):
    """Split a JSON blob into a few deltas to mimic streaming chunks."""
    text = json.dumps(obj)
    mid = len(text) // 2
    return [text[:mid], text[mid:]]


# --- happy path ----------------------------------------------------------

def test_parses_clean_json():
    run_fn, _ = make_run_fn(
        json_deltas({"problem_type": "two_pointers", "topics": ["arrays", "hash_map"]})
    )
    result = classifier.classify("Two Sum problem text", run_fn=run_fn)
    assert result.problem_type == "two_pointers"
    assert result.topics == ["arrays", "hash_map"]


def test_problem_type_is_slugified():
    # Claude returns a human label; classifier must normalise it to a slug.
    run_fn, _ = make_run_fn(
        json_deltas({"problem_type": "Two Pointers!", "topics": ["Arrays"]})
    )
    result = classifier.classify("text", run_fn=run_fn)
    assert result.problem_type == "two_pointers"
    assert "/" not in result.problem_type and " " not in result.problem_type


def test_tolerates_code_fences_and_prose():
    blob = (
        "Sure! Here is the classification:\n"
        "```json\n"
        '{"problem_type": "sliding_window", "topics": ["strings", "two_pointers"]}\n'
        "```\n"
        "Hope that helps."
    )
    run_fn, _ = make_run_fn([blob])
    result = classifier.classify("text", run_fn=run_fn)
    assert result.problem_type == "sliding_window"
    assert "strings" in result.topics


def test_tolerates_extra_keys_and_missing_topics():
    run_fn, _ = make_run_fn(
        json_deltas({"problem_type": "dynamic_programming", "explanation": "blah"})
    )
    result = classifier.classify("text", run_fn=run_fn)
    assert result.problem_type == "dynamic_programming"
    assert result.topics == []  # missing topics -> empty list, not a crash


# --- fallback ------------------------------------------------------------

def test_falls_back_on_garbage():
    run_fn, _ = make_run_fn(["this is not json at all, sorry"])
    result = classifier.classify("text", run_fn=run_fn)
    assert result.problem_type == "uncategorized"
    assert result.topics == []


def test_falls_back_on_empty_stream():
    run_fn, _ = make_run_fn([])
    result = classifier.classify("text", run_fn=run_fn)
    assert result.problem_type == "uncategorized"
    assert result.topics == []


def test_falls_back_when_json_has_no_problem_type():
    run_fn, _ = make_run_fn(json_deltas({"topics": ["arrays"]}))
    result = classifier.classify("text", run_fn=run_fn)
    # no usable type -> safe fallback, but topics may still be recovered
    assert result.problem_type == "uncategorized"
    assert result.topics == ["arrays"]


# --- the problem actually reaches the prompt -----------------------------

def test_problem_text_is_in_the_prompt():
    run_fn, calls = make_run_fn(
        json_deltas({"problem_type": "graph", "topics": ["bfs"]})
    )
    classifier.classify("UNIQUE_PROBLEM_MARKER_123", run_fn=run_fn)
    assert len(calls) == 1
    assert "UNIQUE_PROBLEM_MARKER_123" in calls[0]["prompt"]


def test_default_run_fn_is_claude_cli_run():
    # Signature sanity: classify defaults run_fn to claude_cli.run so production
    # code needs no wiring, while tests can override it.
    import inspect
    import claude_cli
    sig = inspect.signature(classifier.classify)
    assert sig.parameters["run_fn"].default is claude_cli.run
