"""Tests for SP7 (audit6 P2-9 / P2-13 / P2-12 + testing gap 5).

* P2-9  — a failed/errored sandbox verification appends a per-sample detail
          block (input / expected / got / stderr) to the SAVED reasoning ``.md``
          for BOTH Answer and Guided; the stream keeps only the one-line verdict.
* P2-13 — the answer body is code-extracted exactly ONCE per run (the old code
          extracted in the route and again inside ``_verify_code``).
* P2-12 — the Learning prompt interpolates at most the most recent
          ``LEARNED_TOPICS_CAP`` already-learned topics, not the whole index.
* gap 5 — the ✓/✗ verdict line appears BOTH in the SSE stream and in the saved
          ``.md`` for Answer and Guided.

Same harness as ``test_web.py`` / ``test_modes.py``: ``create_app(run_fn=<fake>)``
injects a fake Claude; the sandbox itself runs for real (a genuine python
subprocess against the problem's sample I/O), which is exactly what these tests
need — a solution that actually prints the wrong thing.
"""
from __future__ import annotations

import json

import pytest

import app as app_module
import parsing
import topic_index

CLASSIFY_JSON = {"problem_type": "two_pointers", "topics": ["arrays"]}

# A problem with one parseable sample (mirrors test_sandbox.py's fixture).
PROBLEM_WITH_SAMPLE = (
    "Two Sum: return indices of two numbers adding to target.\n\n"
    "Example 1:\n"
    "Input: nums = [2,7,11,15], target = 9\n"
    "Output: [0,1]\n"
)

PASSING_ANSWER = (
    "Reasoning: echo the known answer.\n\n"
    "```python\n"
    "import sys\n"
    "line = sys.stdin.readline()\n"
    "print('[0,1]')\n"
    "```\n\n"
    "Complexity: O(n).\n"
)

FAILING_ANSWER = (
    "Reasoning: this one is wrong on purpose.\n\n"
    "```python\n"
    "print('[9,9]')\n"
    "```\n\n"
    "Complexity: O(n).\n"
)

GUIDED_FAILING = (
    "# Guided session: Two Sum\n\n"
    "1) Restate. 2) Teach. 3) Reason.\n"
    "4) Answer:\n\n"
    "```python\n"
    "print('[9,9]')\n"
    "```\n"
)


def make_fake_run(mode_markdown, prompt_log=None):
    def fake_run(prompt, **kwargs):
        if prompt_log is not None:
            prompt_log.append(prompt)
        if "Classify the following" in prompt and "Respond with ONLY a tiny" in prompt:
            text = json.dumps(CLASSIFY_JSON)
        else:
            text = mode_markdown
        for i in range(0, len(text), 20):
            yield text[i : i + 20]

    return fake_run


def _make_client(tmp_path, monkeypatch, mode_markdown, prompt_log=None):
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))
    application = app_module.create_app(run_fn=make_fake_run(mode_markdown, prompt_log))
    application.config.update(TESTING=True)
    return application.test_client()


def _parse_sse(body: str):
    text_chunks = []
    events = []
    for block in body.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        event_name = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        data = "\n".join(data_lines)
        if event_name is None:
            text_chunks.append(json.loads(data))
        else:
            events.append((event_name, json.loads(data) if data else None))
    return text_chunks, events


def _post_run(client, mode):
    return client.post(
        "/run",
        json={
            "problem": PROBLEM_WITH_SAMPLE,
            "mode": mode,
            "language": "python",
            "tier": "normal",
        },
    )


def _saved_md(tmp_path, subdir):
    files = [p for p in (tmp_path / subdir).rglob("*.md") if p.is_file()]
    assert files, f"expected a saved .md under {tmp_path / subdir}"
    return files[0].read_text(encoding="utf-8")


# --- P2-9: per-sample failure detail lands in the saved .md ----------------

@pytest.mark.parametrize(
    ("mode", "markdown", "subdir"),
    [
        ("answer", FAILING_ANSWER, "answers"),
        ("guided", GUIDED_FAILING, "guided"),
    ],
)
def test_failed_sample_detail_saved(tmp_path, monkeypatch, mode, markdown, subdir):
    c = _make_client(tmp_path, monkeypatch, markdown)
    resp = _post_run(c, mode)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    text_chunks, events = _parse_sse(body)
    assert [n for (n, _) in events if n == "done"], f"no done event: {events}"

    saved = _saved_md(tmp_path, subdir)
    # the one-line verdict footer is still there...
    assert "**Verification:**" in saved
    assert "✗ Sample tests FAIL" in saved
    # ...followed by the per-sample block: number/status, input, expected, got
    assert "Sample 1" in saved
    assert "fail" in saved
    assert "Input:" in saved
    assert "nums = [2,7,11,15], target = 9" in saved
    assert "Expected:" in saved
    assert "[0,1]" in saved
    assert "Got:" in saved
    assert "[9,9]" in saved

    # the full detail must NOT be streamed to the browser — only the verdict
    streamed = "".join(text_chunks)
    assert "Expected:" not in streamed
    assert "Got:" not in streamed


def test_passing_run_saves_no_detail_block(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, PASSING_ANSWER)
    resp = _post_run(c, "answer")
    assert resp.status_code == 200
    resp.get_data(as_text=True)  # consume the stream so the save happens

    saved = _saved_md(tmp_path, "answers")
    assert "✓ Sample tests PASS" in saved
    assert "Failed samples" not in saved
    assert "Got:" not in saved


# --- P2-13: extract_code runs exactly once per run --------------------------

@pytest.mark.parametrize(
    ("mode", "markdown"),
    [("answer", PASSING_ANSWER), ("guided", GUIDED_FAILING)],
)
def test_extract_code_called_exactly_once(tmp_path, monkeypatch, mode, markdown):
    calls = []
    real = parsing.extract_code

    def counting_extract(markdown_text, language):
        calls.append(language)
        return real(markdown_text, language)

    monkeypatch.setattr(parsing, "extract_code", counting_extract)

    c = _make_client(tmp_path, monkeypatch, markdown)
    resp = _post_run(c, mode)
    assert resp.status_code == 200
    resp.get_data(as_text=True)  # drain the stream (the run happens lazily)

    assert len(calls) == 1, f"extract_code should run once per {mode} run, ran {len(calls)}"


# --- gap 5: verdict line in BOTH the stream and the saved .md ---------------

@pytest.mark.parametrize(
    ("mode", "markdown", "subdir", "verdict"),
    [
        ("answer", PASSING_ANSWER, "answers", "✓ Sample tests PASS"),
        ("answer", FAILING_ANSWER, "answers", "✗ Sample tests FAIL"),
        ("guided", GUIDED_FAILING, "guided", "✗ Sample tests FAIL"),
    ],
)
def test_verdict_line_in_stream_and_saved_md(
    tmp_path, monkeypatch, mode, markdown, subdir, verdict
):
    c = _make_client(tmp_path, monkeypatch, markdown)
    resp = _post_run(c, mode)
    assert resp.status_code == 200
    text_chunks, events = _parse_sse(resp.get_data(as_text=True))

    streamed = "".join(text_chunks)
    assert verdict in streamed, f"verdict missing from stream: {streamed[-200:]!r}"

    saved = _saved_md(tmp_path, subdir)
    assert verdict in saved

    # the done payload carries it too (existing contract, kept)
    done = [p for (n, p) in events if n == "done"]
    assert done and verdict in done[0]["verification"]


# --- P2-12: learned-topics interpolation capped at the most recent 50 -------

def test_learning_prompt_caps_learned_topics(tmp_path, monkeypatch):
    idx = tmp_path / "topic_index.json"
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(idx))
    # seed 60 topics in a known insertion order: topic_01 .. topic_60
    all_topics = [f"topic_{i:02d}" for i in range(1, 61)]
    topic_index.record("seeded", all_topics)

    prompt_log = []
    c = _make_client(tmp_path, monkeypatch, "# Learning doc\n", prompt_log)
    resp = c.post(
        "/run",
        json={"problem": "Two Sum", "mode": "learning", "language": "python"},
    )
    assert resp.status_code == 200
    resp.get_data(as_text=True)  # drain

    learning_prompts = [p for p in prompt_log if "Classify the following" not in p]
    assert learning_prompts, f"no learning prompt captured: {prompt_log}"
    prompt = learning_prompts[0]

    # cap is 50: the MOST RECENT 50 (tail of the insertion-ordered list) are in,
    # the oldest 10 are out
    assert app_module.LEARNED_TOPICS_CAP == 50
    for kept in ("topic_11", "topic_35", "topic_60"):
        assert kept in prompt
    for dropped in ("topic_01", "topic_05", "topic_10"):
        assert dropped not in prompt


def test_known_topics_limit_keeps_most_recent(tmp_path, monkeypatch):
    idx = tmp_path / "topic_index.json"
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(idx))
    topic_index.record("t", ["a", "b", "c", "d"])
    assert topic_index.known_topics(limit=2) == ["c", "d"]
    assert topic_index.known_topics(limit=10) == ["a", "b", "c", "d"]
    assert topic_index.known_topics(limit=0) == []
    assert topic_index.known_topics() == ["a", "b", "c", "d"]
