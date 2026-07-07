"""Tests for the Flask web layer (`app.py`), Answer mode end-to-end.

No real `claude` is ever spawned: ``create_app(run_fn=<fake>)`` injects a fake
that mirrors ``claude_cli.run`` — it accepts ``(prompt, **kwargs)`` and yields
text deltas. The SAME fake covers BOTH Claude calls in the request:

  1. ``classifier.classify`` (asks for a tiny JSON object), and
  2. the Answer stream (asks for markdown with a fenced code block).

so the fake inspects the prompt and returns the right canned payload for each.
The real orchestration (classify -> build prompt -> stream -> split -> save)
still runs, and we assert a file lands under ``output/answers/...`` (pointed at
a tmp dir via ``LEETCOACH_OUTPUT_DIR``).

SSE protocol under test (SP4 reuses it):
  * ``data: <json-string>\n\n``         — a streamed text delta
  * ``event: done\ndata: <json-obj>\n\n``  — terminal success; payload carries
        ``problem_type`` and saved ``paths``
  * ``event: error\ndata: <json-string>\n\n`` — terminal failure
"""
from __future__ import annotations

import json

import pytest

import app as app_module

# --- a fake Claude that answers both the classify and the answer prompt ---

CLASSIFY_JSON = {"problem_type": "two_pointers", "topics": ["arrays"]}

ANSWER_MARKDOWN = (
    "Here is the reasoning. We use a hash map.\n\n"
    "```python\n"
    "def two_sum(nums, target):\n"
    "    seen = {}\n"
    "    for i, n in enumerate(nums):\n"
    "        if target - n in seen:\n"
    "            return [seen[target - n], i]\n"
    "        seen[n] = i\n"
    "```\n\n"
    "Complexity: time O(n), space O(n)."
)


def fake_run(prompt, **kwargs):
    """Mirror claude_cli.run: yield text deltas. Branch on the prompt so one
    fake serves both the classifier call and the answer call."""
    if "Classify the following" in prompt and "Respond with ONLY a tiny" in prompt:
        text = json.dumps(CLASSIFY_JSON)
    else:
        text = ANSWER_MARKDOWN
    # chunk it so the stream is genuinely incremental
    for i in range(0, len(text), 20):
        yield text[i : i + 20]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))
    application = app_module.create_app(run_fn=fake_run)
    application.config.update(TESTING=True)
    return application.test_client(), tmp_path


def _parse_sse(body: str):
    """Split a raw SSE body into (text_chunks, events) where events is a list of
    (event_name, payload)."""
    text_chunks = []
    events = []
    for block in body.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        lines = block.split("\n")
        event_name = None
        data_lines = []
        for line in lines:
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        data = "\n".join(data_lines)
        if event_name is None:
            # plain data: event => a text delta (json-encoded string)
            text_chunks.append(json.loads(data))
        else:
            events.append((event_name, json.loads(data) if data else None))
    return text_chunks, events


# --- GET / ---------------------------------------------------------------

def test_index_serves_page(client):
    c, _ = client
    resp = c.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "<textarea" in html.lower()


# --- POST /run, Answer mode end-to-end -----------------------------------

def test_run_answer_streams_and_saves(client):
    c, tmp_path = client
    resp = c.post(
        "/run",
        json={
            "problem": "Two Sum: return indices of two numbers adding to target.",
            "mode": "answer",
            "language": "python",
            "tier": "normal",
        },
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"

    body = resp.get_data(as_text=True)
    text_chunks, events = _parse_sse(body)

    # streamed text reassembles into the answer markdown
    streamed = "".join(text_chunks)
    assert "def two_sum" in streamed
    assert "Complexity:" in streamed

    # a terminal `done` event arrives, carrying the classification + saved paths
    done = [p for (name, p) in events if name == "done"]
    assert done, f"no done event in: {events}"
    payload = done[0]
    assert payload["problem_type"] == "two_pointers"
    assert payload.get("paths"), "done payload should list saved file paths"
    # no error event on the happy path
    assert not [n for (n, _) in events if n == "error"]

    # a real file was written under output/answers/<type>/...
    answers_dir = tmp_path / "answers"
    written = list(answers_dir.rglob("*"))
    files = [p for p in written if p.is_file()]
    assert files, f"expected a saved answer file under {answers_dir}"
    # the code file should contain the extracted solution
    code_files = [p for p in files if p.suffix == ".py"]
    assert code_files, f"expected a .py code file, got {[p.name for p in files]}"
    assert "def two_sum" in code_files[0].read_text(encoding="utf-8")


# --- validation ----------------------------------------------------------

def test_run_rejects_unknown_mode(client):
    c, _ = client
    resp = c.post(
        "/run",
        json={"problem": "x", "mode": "bogus", "language": "python", "tier": "normal"},
    )
    assert resp.status_code == 400


def test_run_rejects_unknown_tier(client):
    c, _ = client
    resp = c.post(
        "/run",
        json={"problem": "x", "mode": "answer", "language": "python", "tier": "evil"},
    )
    assert resp.status_code == 400


def test_run_rejects_unknown_language(client):
    c, _ = client
    resp = c.post(
        "/run",
        json={"problem": "x", "mode": "answer", "language": "rust", "tier": "normal"},
    )
    assert resp.status_code == 400


def test_run_rejects_empty_problem(client):
    c, _ = client
    resp = c.post(
        "/run",
        json={"problem": "   ", "mode": "answer", "language": "python", "tier": "normal"},
    )
    assert resp.status_code == 400


# --- mid-stream subprocess failure -> SSE error event --------------------

def test_run_answer_midstream_failure_emits_error_event(tmp_path, monkeypatch):
    """If the Claude runner raises *after* yielding some text (the CLI subprocess
    dying mid-response), the stream must not just cut off silently — it must emit
    a terminal ``event: error`` so the browser knows the run failed.

    Regression guard for audit P1 #2 (mid-stream exception not converted to an
    SSE error event).
    """
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))

    def failing_run(prompt, **kwargs):
        # classify call succeeds so we get past step 1 and into the answer stream
        if "Classify the following" in prompt and "Respond with ONLY a tiny" in prompt:
            yield json.dumps(CLASSIFY_JSON)
            return
        # answer stream: yield a couple of real deltas, then the subprocess dies
        yield "Here is the start of the answer"
        yield " with more text"
        raise app_module.claude_cli.ClaudeUnavailableError(
            "`claude` exited with code 1. Is the claude CLI installed?"
        )

    application = app_module.create_app(run_fn=failing_run)
    application.config.update(TESTING=True)
    client = application.test_client()

    resp = client.post(
        "/run",
        json={
            "problem": "Two Sum",
            "mode": "answer",
            "language": "python",
            "tier": "normal",
        },
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    text_chunks, events = _parse_sse(body)

    # the partial text made it to the client before the failure
    assert "".join(text_chunks).startswith("Here is the start")

    # a terminal error event is present, carrying the failure detail...
    errors = [p for (name, p) in events if name == "error"]
    assert errors, f"expected a terminal error event, got events: {events}"
    assert "claude" in errors[0].lower()

    # ...and NO done event was emitted (the run did not succeed)
    assert not [n for (n, _) in events if n == "done"]

    # no partial answer file was saved for a failed run
    answers_dir = tmp_path / "answers"
    saved = [p for p in answers_dir.rglob("*") if p.is_file()] if answers_dir.exists() else []
    assert not saved, f"a failed run should not save files, found: {saved}"


# --- app constructs without a live claude --------------------------------

def test_create_app_does_not_require_claude(monkeypatch):
    # is_available may be False on a CI box; constructing the app must not raise.
    monkeypatch.setattr("claude_cli.is_available", lambda **kw: False)
    application = app_module.create_app(run_fn=fake_run)
    assert application is not None
    # GET / still works (it surfaces the unavailable state in the page, not a crash)
    resp = application.test_client().get("/")
    assert resp.status_code == 200
