"""SP4 web-layer tests: per-run model, save fallback, library cache/hiding,
whole-run delete, security headers, Origin checks, SSE heartbeat, run cancel,
and /healthz. Every Claude call is a fake (no real `claude`)."""
from __future__ import annotations

import json

import pytest
from _helpers import CLASSIFY_JSON, parse_sse

import app as app_module
import claude_cli

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
    monkeypatch.delenv("LEETCOACH_MODEL", raising=False)
    return out


def _client(run_fn):
    application = app_module.create_app(run_fn=run_fn, auth_probe=_authed)
    application.config.update(TESTING=True)
    return application.test_client()


RUN = {"problem": "Two Sum\nExample: Input: 1\nOutput: 1", "mode": "learning",
       "language": "python"}


# --- B12: per-run model -------------------------------------------------------

def test_run_passes_a_valid_per_run_model_to_the_study_call(env):
    rec = Recorder()
    body = _client(rec).post("/run", json={**RUN, "model": "sonnet"}).get_data(as_text=True)
    assert parse_sse(body)[1][-1][0] == "done"
    assert rec.calls[0]["model"] == "sonnet"


def test_run_without_model_uses_the_configured_default(env):
    rec = Recorder()
    _client(rec).post("/run", json=RUN).get_data(as_text=True)
    assert rec.calls[0].get("model") in (None, "opus")


@pytest.mark.parametrize("bad", ["gpt-4", "--dangerous", 5, ["opus"]])
def test_run_rejects_a_model_outside_the_allowlist(env, bad):
    rec = Recorder()
    resp = _client(rec).post("/run", json={**RUN, "model": bad})
    assert resp.status_code == 400
    assert rec.calls == []


def test_config_model_reports_an_unreadable_env_without_wiping_it(env, tmp_path):
    envfile = tmp_path / "picker.env"
    original = b"KEEP=\xff\xfe\xfa\n"
    envfile.write_bytes(original)
    application = app_module.create_app(run_fn=Recorder(), auth_probe=_authed)
    application.config["DOTENV_PATH"] = str(envfile)
    resp = application.test_client().post("/config/model", json={"model": "haiku"})
    assert resp.status_code == 500
    assert envfile.read_bytes() == original


# --- B25: a save OSError falls back to output/_unsorted -----------------------

def _boom(*args, **kwargs):
    raise OSError(36, "File name too long")


@pytest.mark.parametrize("mode,saver", [
    ("learning", "save_learning"),
    ("guided", "save_guided"),
    ("answer", "save_answer"),
])
def test_save_oserror_falls_back_to_unsorted_with_a_clear_message(env, monkeypatch, mode, saver):
    import storage

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
    import storage

    monkeypatch.setattr(storage, "save_learning", _boom)
    monkeypatch.setattr(storage, "save_unsorted", _boom)
    body = _client(Recorder()).post("/run", json=RUN).get_data(as_text=True)
    name, msg = parse_sse(body)[1][-1]
    assert name == "error"
    assert "could not be saved" in msg.lower()
