"""Choosing the model in the web layer: the per-run model a ``/run`` carries
(B12), a pinned ``.env`` model id that survives its own picker alias (I1), the
page always sending the picker's model, and ``POST /config/model`` refusing to
rewrite an ``.env`` it cannot read. Every Claude call is a fake."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from _helpers import CLASSIFY_JSON, parse_sse

from leetcoach import app as app_module
from leetcoach import claude_cli

ROOT = Path(__file__).resolve().parent.parent
RUN = {"problem": "Two Sum\nExample: Input: 1\nOutput: 1", "mode": "learning",
       "language": "python"}


def _is_classify(prompt: str) -> bool:
    return "Classify the following" in prompt


def _authed():
    return claude_cli.AuthStatus(installed=True, logged_in=True)


class Recorder:
    def __init__(self, text="# Notes\n"):
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


def _app_js() -> str:
    return (ROOT / "leetcoach" / "static" / "app.js").read_text(encoding="utf-8")


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


# --- I1: a pinned model id is not overridden by its own picker alias -----------

def test_posting_the_configured_alias_runs_the_pinned_id(env, monkeypatch):
    monkeypatch.setenv("LEETCOACH_MODEL", "claude-sonnet-4-5")
    rec = Recorder()
    body = _client(rec).post("/run", json={**RUN, "model": "sonnet"}).get_data(as_text=True)
    assert parse_sse(body)[1][-1][0] == "done"
    assert rec.calls[0]["model"] == "claude-sonnet-4-5"


def test_posting_a_different_alias_still_switches_model(env, monkeypatch):
    monkeypatch.setenv("LEETCOACH_MODEL", "claude-sonnet-4-5")
    rec = Recorder()
    _client(rec).post("/run", json={**RUN, "model": "haiku"}).get_data(as_text=True)
    assert rec.calls[0]["model"] == "haiku"


def test_client_always_sends_the_active_picker_model():
    js = _app_js()
    # Every run carries the model the tab shows (B12): the server maps the alias
    # that matches a pinned .env id back to that id (resolve_run_model, I1), so
    # there is no need to withhold it until the picker is clicked.
    assert re.search(r"if \(model\) body\.model = model;", js)
    assert "modelTouched" not in js
