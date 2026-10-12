"""What a ``POST /run`` stream reports beyond the Answer-mode happy path in
``test_web.py``: ``phase`` and ``meta`` events (the concrete model id),
verification progress, a stream cut short before its final ``result`` event
(study run, classifier, Quick Ask - B3) and a save that fails over to
``output/_unsorted`` (B25). Every Claude call is a fake."""
from __future__ import annotations

import json

import pytest
from _helpers import CLASSIFY_JSON, parse_sse

from leetcoach import app as app_module
from leetcoach import claude_cli, sandbox

RUN = {"problem": "Two Sum\nExample: Input: 1\nOutput: 1", "mode": "learning",
       "language": "python"}
ANSWER = {**RUN, "mode": "answer", "tier": "normal"}
DOC = "Answer.\n\n```python solution\nprint(input())\n```\n"
ANSWER_MD = (
    "Use a hash map.\n\n"
    "```python solution\n"
    "import sys\nprint(sys.stdin.read().strip())\n"
    "```\n"
)


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


class Recorder:
    """A fake run_fn that records the kwargs of every non-classifier call."""

    def __init__(self, text=ANSWER_MD):
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
    return out


def _client(run_fn):
    application = app_module.create_app(run_fn=run_fn, auth_probe=_authed)
    application.config.update(TESTING=True)
    return application.test_client()


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


# --- B3: no terminal `result` event -> incomplete, never a success -----------

def _delta(text):
    return json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})


def _result(text=""):
    return json.dumps({"type": "result", "subtype": "success", "result": text})


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


# --- B25: a save OSError falls back to output/_unsorted -----------------------

def _boom(*args, **kwargs):
    raise OSError(36, "File name too long")


@pytest.mark.parametrize("mode,saver", [
    ("learning", "save_learning"),
    ("guided", "save_guided"),
    ("answer", "save_answer"),
])
def test_save_oserror_falls_back_to_unsorted_with_a_clear_message(env, monkeypatch, mode, saver):
    from leetcoach import storage

    monkeypatch.setattr(storage, saver, _boom)
    payload = {**RUN, "mode": mode, "tier": "normal"}
    body = _client(Recorder()).post("/run", json=payload).get_data(as_text=True)
    name, done = parse_sse(body)[1][-1]
    assert name == "done"
    assert len(done["paths"]) == 1
    saved = done["paths"][0]
    assert "_unsorted" in saved and saved.endswith(".md")
    from pathlib import Path
    text = Path(saved).read_text(encoding="utf-8")
    assert "Use a hash map." in text           # the streamed answer is kept
    if mode == "answer":
        assert "```python solution" in text    # the code travels inside the .md
    assert "File name too long" in done["save_warning"]
    assert "_unsorted" in done["save_warning"]


def test_save_and_fallback_both_failing_is_a_terminal_error(env, monkeypatch):
    from leetcoach import storage

    monkeypatch.setattr(storage, "save_learning", _boom)
    monkeypatch.setattr(storage, "save_unsorted", _boom)
    body = _client(Recorder()).post("/run", json=RUN).get_data(as_text=True)
    name, msg = parse_sse(body)[1][-1]
    assert name == "error"
    assert "could not be saved" in msg.lower()
