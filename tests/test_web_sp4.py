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
