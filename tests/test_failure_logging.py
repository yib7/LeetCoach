"""3A G2: an expected CLI failure (not installed, signed out, offline: the CLI
said what is wrong and the page shows it) is logged as one line, not as a
traceback in the console window LeetCoach.cmd keeps open; anything unexpected
still logs its full traceback (audit P2-8)."""
from __future__ import annotations

import logging

import app as app_module
import claude_cli

PROBLEM = ("1. Two Sum\nGiven nums and target, return indices.\n"
           "Input: nums = [2,7], target = 9\nOutput: [0,1]\n")
RUN = {"problem": PROBLEM, "language": "python", "mode": "answer", "tier": "normal"}


def _client(exc):
    def run_fn(*a, **k):
        raise exc
        yield  # pragma: no cover - makes this a generator

    application = app_module.create_app(run_fn=run_fn, auth_probe=lambda: None)
    application.testing = True
    return application.test_client()


def _post(client, path, body):
    return client.post(path, json=body, headers={"Origin": "http://localhost"})


def _records(caplog, needle):
    return [r for r in caplog.records if needle in r.getMessage()]


def test_an_expected_cli_failure_is_one_line_in_the_log(caplog):
    exc = claude_cli.ClaudeUnavailableError("The `claude` CLI was not found on PATH.")
    client = _client(exc)
    with caplog.at_level(logging.INFO):
        body = _post(client, "/run", RUN).get_data(as_text=True)
        ask = _post(client, "/ask", {"question": "why?", "problem": PROBLEM})
    assert "Run failed: The `claude` CLI was not found on PATH." in body
    assert ask.status_code == 502
    for needle in ("run failed", "quick ask failed"):
        recs = _records(caplog, needle)
        assert recs, needle
        assert all(r.exc_info is None for r in recs), needle
        assert all("not found on PATH" in r.getMessage() for r in recs), needle


def test_an_unexpected_failure_still_logs_its_traceback(caplog):
    client = _client(ValueError("boom"))
    with caplog.at_level(logging.INFO):
        body = _post(client, "/run", RUN).get_data(as_text=True)
    assert "Run failed: boom" in body
    recs = _records(caplog, "run failed")
    assert recs and all(r.exc_info for r in recs)
