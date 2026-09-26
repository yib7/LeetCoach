"""Test-suite-wide environment isolation (B8/#7).

Three problems, all from the audit: (1) ``app.py`` loads the developer's real
``.env`` at import time, so importing ``app`` during collection can leak a real
``LEETCOACH_*`` setting into the test process; (2) any test that forgets to
override ``LEETCOACH_OUTPUT_DIR`` / ``LEETCOACH_TOPIC_INDEX`` falls through to
the real ``output/`` tree and writes fake study material / topics into it;
(3) ``create_app()`` defaults ``app.config["DOTENV_PATH"]`` to the real project
``.env`` — a test that calls ``/config/model`` (directly, or via a route that
happens to touch it) without explicitly overriding ``app.config`` afterward
would WRITE to that real file.

The ``LEETCOACH_NO_DOTENV`` line below runs as module-level code, not inside a
fixture, so it executes when pytest imports this file during collection —
BEFORE any ``test_*.py`` module's ``import app`` triggers app.py's module-level
``load_dotenv(...)`` call. That ordering is what makes the real .env
unreachable for the whole session (see ``app.py``, which honours this var).

The autouse fixture below re-asserts isolation per test (belt and suspenders:
a test could otherwise delete the env var mid-suite) and gives every test a
private output directory so a test that forgets to set ``LEETCOACH_OUTPUT_DIR``
still can't touch the real study library. It also points
``LEETCOACH_DOTENV_PATH`` at a private tmp file — ``app.create_app()`` honours
it as the default for ``app.config["DOTENV_PATH"]`` (#7) — so EVERY
``create_app()`` call in the suite is redirected away from the real ``.env`` by
default, not just the couple of tests that happen to override
``app.config["DOTENV_PATH"]`` by hand. Finally (A7) it points the neutral
``claude`` working directory at a tmp path and replaces the ``claude --help``
flag probe with canned text, so no test ever spawns the real CLI or creates the
real ``%LOCALAPPDATA%`` directory. Tests that need a specific location call
``monkeypatch.setenv(...)`` / set ``application.config[...]`` themselves
afterward and win, since that happens later in the same fixture-teardown stack.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from _helpers import FAKE_CLAUDE_HELP

import claude_cli

# See module docstring: must run at import time, not inside a fixture.
os.environ.setdefault("LEETCOACH_NO_DOTENV", "1")

_LEETCOACH_PREFIX = "LEETCOACH_"
NONEXISTENT_CLAUDE_BIN = "leetcoach-test-no-such-claude-binary-7f3a"


@pytest.fixture(autouse=True)
def _isolate_leetcoach_env(tmp_path, monkeypatch):
    """Strip every ``LEETCOACH_*`` var and point the output dir + the model
    picker's dotenv target at private tmp paths before each test runs
    (B8/#7)."""
    for key in list(os.environ):
        if key.startswith(_LEETCOACH_PREFIX):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LEETCOACH_NO_DOTENV", "1")
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path / "leetcoach-output"))
    # #7: no test (whichever create_app() call it uses) can ever write the
    # real project `.env`, even if it never touches app.config itself.
    monkeypatch.setenv("LEETCOACH_DOTENV_PATH", str(tmp_path / ".env.leetcoach-test"))
    # A7: the neutral directory `claude` runs in defaults to the real
    # %LOCALAPPDATA%\LeetCoach\claude-cwd — keep every test out of it.
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", str(tmp_path / "claude-cwd"))
    # SP2 M6: fail closed. Any spawn that falls through to the default binary
    # (a runner/probe a test forgot to fake) finds nothing on PATH and errors,
    # instead of reaching the real `claude` CLI and spending subscription
    # usage. Tests that need a resolvable name set their own.
    monkeypatch.setenv("LEETCOACH_CLAUDE_BIN", NONEXISTENT_CLAUDE_BIN)
    # A7: the optional-flag gate probes `claude --help`. No test may spawn the
    # real CLI for that, so feed the probe canned help text and start each
    # test with an empty probe cache.
    monkeypatch.setattr(claude_cli, "_probe_help_text", lambda argv: FAKE_CLAUDE_HELP)
    claude_cli.clear_flag_cache()
    # B1: same for the sign-in probe - `claude auth status` is never spawned
    # (tests that exercise the real runner capture it at import time and
    # point it at a Python stand-in), and the 60 s cache starts empty.
    monkeypatch.setattr(
        claude_cli,
        "_default_auth_runner",
        lambda argv: SimpleNamespace(returncode=0, stdout='{"loggedIn": true}'),
    )
    claude_cli.clear_auth_cache()
    yield
    claude_cli.clear_flag_cache()
    claude_cli.clear_auth_cache()
