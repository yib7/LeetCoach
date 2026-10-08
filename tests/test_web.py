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
import logging
import re

import pytest
from _helpers import CLASSIFY_JSON
from _helpers import parse_sse as _parse_sse

import app as app_module
import claude_cli

# --- a fake Claude that answers both the classify and the answer prompt ---

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


QUICK_ASK_ANSWER = (
    "`heapq.heappush(heap, item)` pushes onto a min-heap.\n\n"
    "```python\nimport heapq\nheapq.heappush(h, 3)\n```"
)

# Quick Ask calls recorded as (prompt, model) so tests can assert on both;
# reset per-test by the `client` fixture (and directly by standalone tests).
QA_CALLS: list[tuple[str, str | None]] = []


def fake_run(prompt, **kwargs):
    """Mirror claude_cli.run: yield text deltas. Branch on the prompt so one
    fake serves the classifier, answer, and quick-ask calls."""
    if "Classify the following" in prompt and "Respond with ONLY a tiny" in prompt:
        text = json.dumps(CLASSIFY_JSON)
    elif "You are a quick-reference assistant" in prompt:
        QA_CALLS.append((prompt, kwargs.get("model")))
        text = QUICK_ASK_ANSWER
    else:
        text = ANSWER_MARKDOWN
    # chunk it so the stream is genuinely incremental
    for i in range(0, len(text), 20):
        yield text[i : i + 20]


def _authed_probe():
    """A signed-in auth probe so the page renders with no CLI banner and never
    spawns the real `claude auth status` during tests."""
    return claude_cli.AuthStatus(installed=True, logged_in=True)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))
    # Register the key monkeypatch owns so a /config/model write (which mutates
    # os.environ directly) is reverted at teardown and can't leak between tests.
    monkeypatch.delenv("LEETCOACH_MODEL", raising=False)
    QA_CALLS.clear()
    application = app_module.create_app(run_fn=fake_run, auth_probe=_authed_probe)
    application.config.update(TESTING=True)
    # Persist model-picker writes to a temp .env, never the real project file.
    application.config["DOTENV_PATH"] = str(tmp_path / ".env")
    return application.test_client(), tmp_path


# --- GET / ---------------------------------------------------------------

def test_index_serves_page(client):
    c, _ = client
    resp = c.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "<textarea" in html.lower()


def test_index_has_quick_ask_panel(client):
    c, _ = client
    resp = c.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="quickask"' in html


def test_security_headers_present(client):
    c, _ = client
    resp = c.get("/")
    csp = resp.headers.get("Content-Security-Policy", "")
    # Strict policy holds because every script/style/font is self-hosted and the
    # renderer only emits inline data: images (defense-in-depth for the
    # untrusted-markdown surface, incl. Quick Ask).
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "img-src 'self' data:" in csp
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


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


# --- non-string JSON fields -> clean 400, not a 500 stack trace (3.12) ------

@pytest.mark.parametrize(
    "payload",
    [
        # problem is not a string -> the .strip()/slice used to blow up with 500
        {"problem": 123, "mode": "answer", "language": "python", "tier": "normal"},
        {"problem": ["x"], "mode": "answer", "language": "python", "tier": "normal"},
        # mode / language / tier are not strings
        {"problem": "x", "mode": 5, "language": "python", "tier": "normal"},
        {"problem": "x", "mode": "answer", "language": 5, "tier": "normal"},
        {"problem": "x", "mode": "answer", "language": "python", "tier": ["normal"]},
    ],
)
def test_run_rejects_non_string_fields(client, payload):
    """A script/curl sending a non-string JSON field must get a clean 400 with an
    ``error`` message, never a 500 stack trace (unauthenticated local endpoint)."""
    c, tmp_path = client
    resp = c.post("/run", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    # a bad-type request must not spend Claude budget or save anything
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


# --- B23: a non-object JSON body -> clean 400 on every JSON route ----------

@pytest.mark.parametrize("payload", [["x"], "just a string", 123, True])
def test_run_rejects_non_object_json_body(client, payload):
    """A JSON array/string/number/bool body (not an object) must be a clean
    400, not a 500 from `.get()` on a list/str (B23)."""
    c, tmp_path = client
    resp = c.post("/run", data=json.dumps(payload), content_type="application/json")
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


@pytest.mark.parametrize("payload", [["x"], "just a string", 123, True])
def test_ask_rejects_non_object_json_body(client, payload):
    c, _ = client
    resp = c.post("/ask", data=json.dumps(payload), content_type="application/json")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


@pytest.mark.parametrize("payload", [["sonnet"], "sonnet", 123])
def test_config_model_rejects_non_object_json_body(client, payload):
    c, tmp_path = client
    resp = c.post("/config/model", data=json.dumps(payload), content_type="application/json")
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert not (tmp_path / ".env").exists()


# --- B24: no extracted code -> no empty code file lands in the library -----

NO_CODE_ANSWER_MARKDOWN = (
    "I couldn't find a runnable solution to show here — this is reasoning "
    "only, with no fenced code block at all."
)


def fake_run_no_code(prompt, **kwargs):
    if "Classify the following" in prompt and "Respond with ONLY a tiny" in prompt:
        text = json.dumps(CLASSIFY_JSON)
    else:
        text = NO_CODE_ANSWER_MARKDOWN
    for i in range(0, len(text), 20):
        yield text[i : i + 20]


def test_run_answer_with_no_code_block_saves_no_empty_code_file(tmp_path, monkeypatch):
    """``extract_code`` returns "" when Claude's answer has no fenced block at
    all; B24 says the run must still save the reasoning .md but must NOT write
    a 0-byte code file that would look like a real (if empty) answer."""
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))
    application = app_module.create_app(run_fn=fake_run_no_code, auth_probe=_authed_probe)
    application.config.update(TESTING=True)
    application.config["DOTENV_PATH"] = str(tmp_path / ".env")
    c = application.test_client()

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
    body = resp.get_data(as_text=True)
    _text_chunks, events = _parse_sse(body)
    done = [p for (name, p) in events if name == "done"]
    assert done, f"no done event in: {events}"
    paths = done[0].get("paths") or []
    assert paths, "reasoning file should still be saved"
    assert None not in paths

    answers_dir = tmp_path / "answers"
    files = [p for p in answers_dir.rglob("*") if p.is_file()]
    code_files = [p for p in files if p.suffix == ".py"]
    assert not code_files, f"expected no .py code file, got {[p.name for p in files]}"
    md_files = [p for p in files if p.suffix == ".md"]
    assert md_files, "expected the reasoning .md to still be saved"


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


# --- Host-header guard (DNS-rebinding defense, audit6 P1-3) ---------------

RUN_PAYLOAD = {
    "problem": "Two Sum: return indices of two numbers adding to target.",
    "mode": "answer",
    "language": "python",
    "tier": "normal",
}


@pytest.mark.parametrize(
    "host",
    [
        "evil.com",
        "evil.com:5000",
        "127.0.0.1.evil.com",
        "localhost.evil.com:5000",
    ],
)
def test_run_rejects_foreign_host(client, host):
    """A DNS-rebinding page reaches 127.0.0.1 with an attacker-controlled Host
    header. /run must refuse it before spending any Claude budget."""
    c, tmp_path = client
    resp = c.post("/run", json=RUN_PAYLOAD, headers={"Host": host})
    assert resp.status_code == 403
    assert "host" in resp.get_json()["error"].lower()
    # nothing ran, nothing saved
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


def test_index_rejects_foreign_host(client):
    c, _ = client
    resp = c.get("/", headers={"Host": "evil.com"})
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:5000",
        "Localhost:5000",  # Host headers are case-insensitive
        "127.0.0.1",
        "127.0.0.1:8080",  # any port on a loopback hostname is fine
        "[::1]",
        "[::1]:5000",
    ],
)
def test_index_allows_loopback_hosts(client, host):
    c, _ = client
    resp = c.get("/", headers={"Host": host})
    assert resp.status_code == 200


# --- empty Claude answer -> SSE error, nothing saved (audit6 P2-1) ---------

@pytest.mark.parametrize("mode", ["answer", "learning", "guided"])
def test_run_empty_stream_emits_error_and_saves_nothing(tmp_path, monkeypatch, mode):
    """If the runner yields zero deltas without raising, that is a failure, not
    an empty success: the stream must end in ``event: error`` and no files may
    be written."""
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))

    def empty_run(prompt, **kwargs):
        # classify call succeeds so we get past step 1 and into the mode stream
        if "Classify the following" in prompt and "Respond with ONLY a tiny" in prompt:
            yield json.dumps(CLASSIFY_JSON)
        # every other prompt: yield nothing (Claude produced no text)

    application = app_module.create_app(run_fn=empty_run)
    application.config.update(TESTING=True)
    c = application.test_client()

    resp = c.post("/run", json={**RUN_PAYLOAD, "mode": mode})
    assert resp.status_code == 200
    _, events = _parse_sse(resp.get_data(as_text=True))

    errors = [p for (name, p) in events if name == "error"]
    assert errors, f"expected a terminal error event, got events: {events}"
    assert "empty answer" in errors[0].lower()
    assert not [n for (n, _) in events if n == "done"]

    # NO files at all for the empty case (no empty .md/.py husks)
    saved = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert not saved, f"an empty run should not save files, found: {saved}"


# --- run failure logs the traceback server-side (audit6 P2-8) --------------

def test_run_failure_logs_traceback(tmp_path, monkeypatch, caplog):
    """The last-resort handler must log the full exception (traceback included)
    via app.logger before yielding the short SSE error message."""
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))

    def failing_run(prompt, **kwargs):
        if "Classify the following" in prompt and "Respond with ONLY a tiny" in prompt:
            yield json.dumps(CLASSIFY_JSON)
            return
        yield "partial text"
        raise RuntimeError("boom from claude")

    application = app_module.create_app(run_fn=failing_run)
    application.config.update(TESTING=True)
    c = application.test_client()

    with caplog.at_level(logging.ERROR):
        resp = c.post("/run", json=RUN_PAYLOAD)
        body = resp.get_data(as_text=True)  # consume the stream inside the capture

    # the client still gets only the short message (unchanged behaviour)...
    _, events = _parse_sse(body)
    errors = [p for (name, p) in events if name == "error"]
    assert errors and "boom from claude" in errors[0]

    # ...while the server log keeps the exception WITH its traceback
    records = [r for r in caplog.records if "run failed" in r.getMessage()]
    assert records, f"expected a 'run failed' log record, got: {caplog.records}"
    assert records[0].getMessage() == "run failed (mode=answer)"
    assert records[0].exc_info, "log record should carry exc_info (traceback)"
    assert "Traceback" in caplog.text
    assert "boom from claude" in caplog.text


# --- POST /ask (Quick Ask, SP2) -------------------------------------------

def test_ask_returns_answer(client):
    c, _ = client
    resp = c.post("/ask", json={"question": "How does heapq.heappush work?"})
    assert resp.status_code == 200
    assert resp.get_json() == {"answer": QUICK_ASK_ANSWER}


def test_ask_uses_quick_ask_model(client):
    c, _ = client
    resp = c.post("/ask", json={"question": "syntax of heappush?"})
    assert resp.status_code == 200
    assert QA_CALLS, "expected the quick-ask prompt to reach run_fn"
    _, model = QA_CALLS[0]
    assert model == "haiku"


@pytest.mark.parametrize("question", [None, "", "   "])
def test_ask_rejects_missing_or_blank_question(client, question):
    c, _ = client
    payload = {} if question is None else {"question": question}
    resp = c.post("/ask", json=payload)
    assert resp.status_code == 400
    assert "question" in resp.get_json()["error"].lower()


def test_ask_rejects_oversized_question_but_allows_500(client):
    c, _ = client
    resp = c.post("/ask", json={"question": "x" * 501})
    assert resp.status_code == 400
    resp = c.post("/ask", json={"question": "x" * 500})
    assert resp.status_code == 200


def test_ask_rejects_unknown_language(client):
    c, _ = client
    resp = c.post("/ask", json={"question": "q?", "language": "rust"})
    assert resp.status_code == 400
    assert "language" in resp.get_json()["error"].lower()


def test_ask_defaults_language_to_python(client):
    c, _ = client
    resp = c.post("/ask", json={"question": "what does zip do?"})
    assert resp.status_code == 200
    prompt, _ = QA_CALLS[0]
    assert "Python" in prompt


def test_ask_run_failure_returns_502(tmp_path, monkeypatch):
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))

    def failing_run(prompt, **kwargs):
        raise app_module.claude_cli.ClaudeUnavailableError("claude fell over")
        yield  # pragma: no cover - makes this a generator like the real runner

    application = app_module.create_app(run_fn=failing_run)
    application.config.update(TESTING=True)
    resp = application.test_client().post("/ask", json={"question": "q?"})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


@pytest.mark.parametrize("chunks", [[], ["  ", "\n"]])
def test_ask_empty_answer_returns_502(tmp_path, monkeypatch, chunks):
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))

    def empty_run(prompt, **kwargs):
        yield from chunks

    application = app_module.create_app(run_fn=empty_run)
    application.config.update(TESTING=True)
    resp = application.test_client().post("/ask", json={"question": "q?"})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_ask_problem_lands_inside_context_fence(client):
    c, _ = client
    resp = c.post(
        "/ask",
        json={"question": "q?", "problem": "Two Sum: find indices."},
    )
    assert resp.status_code == 200
    prompt, _ = QA_CALLS[0]
    # B21: the fence lines carry a per-prompt nonce.
    opened = re.search(
        r"^--- BEGIN PROBLEM CONTEXT \(do not solve\) ([0-9a-f]+) ---$", prompt, re.MULTILINE
    )
    assert opened
    close_fence = f"\n--- END PROBLEM CONTEXT {opened.group(1)} ---"
    inside = prompt[opened.end():prompt.index(close_fence, opened.end())]
    assert "Two Sum: find indices." in inside


def test_ask_truncates_oversized_problem(client):
    c, _ = client
    big = "A" * 7000
    resp = c.post("/ask", json={"question": "q?", "problem": big})
    assert resp.status_code == 200
    prompt, _ = QA_CALLS[0]
    assert "A" * app_module.QUICK_ASK_PROBLEM_CONTEXT_CAP in prompt
    assert big not in prompt


@pytest.mark.parametrize(
    "payload",
    [
        # question is not a string -> the .strip() used to blow up with 500
        {"question": 123},
        {"question": ["a"]},
        # problem is not a string -> the CAP slice (outside try/except) used to 500
        {"question": "hi", "problem": 999},
        {"question": "hi", "problem": ["x"]},
        # language is not a string -> the .strip().lower() used to 500
        {"question": "hi", "language": 5},
    ],
)
def test_ask_rejects_non_string_fields(client, payload):
    """Non-string JSON fields to /ask must return a clean 400 with an ``error``
    message, never a 500 (the problem-context slice sits outside the try/except)."""
    c, _ = client
    resp = c.post("/ask", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- config knob: quick_ask_model (tested next to its consumer) ------------

def test_quick_ask_model_knob_defaults_to_haiku(monkeypatch):
    import config

    monkeypatch.delenv("LEETCOACH_QUICK_ASK_MODEL", raising=False)
    assert config.quick_ask_model() == "haiku"
    monkeypatch.setenv("LEETCOACH_QUICK_ASK_MODEL", "sonnet")
    assert config.quick_ask_model() == "sonnet"


# --- P2-3: oversized POST rejected with 413 ------------------------------

def test_oversized_run_post_is_rejected_413(client):
    c, _ = client
    big = "x" * (2 * 1024 * 1024 + 1024)  # just over the 2 MiB cap
    resp = c.post(
        "/run",
        json={"problem": big, "mode": "answer", "language": "python", "tier": "normal"},
    )
    assert resp.status_code == 413, resp.status_code


# --- P2-12: in-flight /run de-duplication (409) --------------------------

_RUN_PAYLOAD = {
    "problem": "Two Sum: return indices adding to target.",
    "mode": "answer",
    "language": "python",
    "tier": "normal",
}


def test_duplicate_inflight_run_is_rejected_409_then_freed(client):
    """A second identical run while the first is still streaming gets a 409;
    once the first stream is drained the key frees and an identical run works."""
    c, _ = client
    # First request: the view registers the in-flight key and returns a streaming
    # Response whose body generator has NOT run yet (lazy — consumed on get_data).
    r1 = c.post("/run", json=_RUN_PAYLOAD)
    assert r1.status_code == 200
    # Second identical request while r1 is in-flight -> 409.
    r2 = c.post("/run", json=_RUN_PAYLOAD)
    assert r2.status_code == 409, r2.status_code
    assert "already in progress" in r2.get_json()["error"]
    # Drain r1 -> its generator finally releases the key.
    r1.get_data(as_text=True)
    # A later identical request now succeeds (key was freed).
    r3 = c.post("/run", json=_RUN_PAYLOAD)
    assert r3.status_code == 200, r3.status_code
    r3.get_data(as_text=True)  # drain so it saves + releases


def test_distinct_keys_do_not_collide(client):
    """Different (problem/mode/language/tier) tuples never dedupe each other."""
    c, _ = client
    r1 = c.post("/run", json=_RUN_PAYLOAD)
    assert r1.status_code == 200
    other = dict(_RUN_PAYLOAD, tier="optimal")  # distinct valid key
    r2 = c.post("/run", json=other)
    assert r2.status_code == 200, r2.status_code  # no false 409 across distinct keys
    # Drain LIFO — two stream_with_context responses held open share one request-
    # context stack in the test client, so pop them last-in-first-out.
    r2.get_data(as_text=True)
    r1.get_data(as_text=True)


# --- P2-6: /library listing is cached and invalidates on change ----------

def test_library_listing_is_cached_and_invalidates_on_change(client, monkeypatch):
    import os
    c, tmp = client
    calls = []
    real = app_module._library_files

    def counting(root):
        calls.append(1)
        return real(root)

    monkeypatch.setattr(app_module, "_library_files", counting)

    (tmp / "a.md").write_text("x", encoding="utf-8")
    assert c.get("/library").status_code == 200          # miss -> walks
    n1 = len(calls)
    assert n1 >= 1
    c.get("/library")                                    # same sig -> cache hit
    assert len(calls) == n1, "unchanged library should serve from cache"

    (tmp / "b.md").write_text("y", encoding="utf-8")
    os.utime(tmp)  # bump root mtime deterministically (coarse-clock safety)
    c.get("/library")                                    # sig changed -> re-walk
    assert len(calls) == n1 + 1, "a change must invalidate the cache"


# --- P2-11: DELETE /library/file -----------------------------------------

def test_delete_removes_contained_file_and_refreshes_listing(client):
    c, tmp = client
    f = tmp / "answers" / "hash_map" / "two_sum__normal.md"
    f.parent.mkdir(parents=True)
    f.write_text("# solution", encoding="utf-8")

    listed = c.get("/library").get_json()["files"]
    rel = "answers/hash_map/two_sum__normal.md"
    assert any(x["path"] == rel for x in listed)

    resp = c.delete("/library/file?path=" + rel)
    assert resp.status_code == 200, resp.status_code
    assert resp.get_json()["deleted"] is True
    assert not f.exists()

    # listing refreshes (cache invalidated) — the file is gone
    listed2 = c.get("/library").get_json()["files"]
    assert all(x["path"] != rel for x in listed2)


def test_delete_rejects_path_traversal(client):
    c, tmp = client
    victim = tmp.parent / "leetcoach_delete_victim.md"
    victim.write_text("keep me", encoding="utf-8")
    try:
        resp = c.delete("/library/file?path=" + "../leetcoach_delete_victim.md")
        assert resp.status_code == 404, resp.status_code   # uniform no-leak reject
        assert victim.exists(), "a traversal must never delete outside the root"
    finally:
        if victim.exists():
            victim.unlink()


def test_delete_missing_or_bad_suffix_is_404(client):
    c, tmp = client
    # nonexistent library file
    assert c.delete("/library/file?path=answers/none.md").status_code == 404
    # a real file but a non-library suffix (.log ∉ LIBRARY_EXTENSIONS) must not
    # be deletable through the library endpoint
    bad = tmp / "notes.log"
    bad.write_text("x", encoding="utf-8")
    assert c.delete("/library/file?path=notes.log").status_code == 404
    assert bad.exists()


# --- app constructs without a live claude --------------------------------

def test_create_app_does_not_require_claude():
    # claude may be absent on a CI box; constructing the app AND serving the page
    # must not raise. A probe reporting "not installed" stands in for that box.
    def missing_probe():
        return claude_cli.AuthStatus(installed=False, logged_in=False)

    application = app_module.create_app(run_fn=fake_run, auth_probe=missing_probe)
    assert application is not None
    # GET / still works (it surfaces the unavailable state in the page, not a crash)
    resp = application.test_client().get("/")
    assert resp.status_code == 200


# --- auth / model flags injected into the page ---------------------------

def test_index_injects_signed_in_and_model_flags(client):
    c, _ = client  # the fixture's probe reports installed + logged_in
    html = c.get("/").get_data(as_text=True)
    assert 'data-claude-available="true"' in html
    assert 'data-claude-logged-in="true"' in html
    # default model (the `opus` alias) maps to the picker's "opus"
    assert 'data-claude-model="opus"' in html
    # no unreplaced placeholders leak to the page
    assert "__CLAUDE_AVAILABLE__" not in html
    assert "__CLAUDE_LOGGED_IN__" not in html
    assert "__CLAUDE_MODEL__" not in html


def test_index_picker_offers_fable_and_shows_latest_versions(client):
    c, _ = client
    html = c.get("/").get_data(as_text=True)
    for alias in ("fable", "opus", "sonnet", "haiku"):
        assert f'data-val="{alias}"' in html
    # display-only version labels come from config.LATEST_MODEL_IDS
    assert 'title="Fable 5.1' in html
    assert 'title="Opus 5.5"' in html
    assert "__MODEL_LABEL_" not in html


def test_index_signed_out_flag_is_false():
    def signed_out():
        return claude_cli.AuthStatus(installed=True, logged_in=False)

    application = app_module.create_app(run_fn=fake_run, auth_probe=signed_out)
    html = application.test_client().get("/").get_data(as_text=True)
    assert 'data-claude-available="true"' in html
    assert 'data-claude-logged-in="false"' in html


# --- POST /config/model: persist the picked model ------------------------

def test_config_model_persists_and_takes_effect(client):
    c, tmp_path = client
    resp = c.post("/config/model", json={"model": "sonnet"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "model": "sonnet"}
    # written to the (temp) .env for a future launch ...
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "LEETCOACH_MODEL=sonnet" in env_text
    # ... and live in the process so the next run uses it with no restart
    import config
    assert config.model() == "sonnet"


def test_config_model_accepts_fable(client):
    c, tmp_path = client
    resp = c.post("/config/model", json={"model": "fable"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "model": "fable"}
    assert "LEETCOACH_MODEL=fable" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_config_model_rejects_unknown_alias(client):
    c, tmp_path = client
    resp = c.post("/config/model", json={"model": "gpt-4"})
    assert resp.status_code == 400
    # nothing persisted on a rejected value
    assert not (tmp_path / ".env").exists()


def test_config_model_rejects_missing_field(client):
    c, _ = client
    resp = c.post("/config/model", json={})
    assert resp.status_code == 400


# --- #7: create_app() must never default to the real project .env ---------

def test_create_app_default_dotenv_path_is_not_the_real_project_env():
    """A test that calls ``create_app()`` and never overrides
    ``app.config["DOTENV_PATH"]`` by hand (unlike the ``client`` fixture
    above) must still be unable to write the real repo ``.env`` — the
    tests/conftest.py autouse fixture points ``LEETCOACH_DOTENV_PATH`` at a
    private tmp file, and ``app.create_app()`` honours it as the default."""
    from pathlib import Path

    application = app_module.create_app(run_fn=fake_run, auth_probe=_authed_probe)
    real_env = Path(app_module.__file__).resolve().parent / ".env"
    assert application.config["DOTENV_PATH"] != str(real_env)

    # Snapshot the real .env (which may genuinely exist -- this is a real dev's
    # working checkout) rather than asserting on its content, so the test
    # can't produce a false failure/pass depending on what's already in it.
    before = real_env.read_text(encoding="utf-8") if real_env.exists() else None

    resp = application.test_client().post(
        "/config/model", json={"model": "opus"}
    )
    assert resp.status_code == 200

    after = real_env.read_text(encoding="utf-8") if real_env.exists() else None
    assert after == before, "the real project .env must be untouched by create_app()"
