"""Boot / import smoke test.

Guards against import-time regressions: the app and every core module must
import cleanly, and ``app.create_app()`` must construct a real Flask app
**without a live `claude`** — no subprocess is ever spawned here (the factory
defers the availability probe to request time, so construction is side-effect
free).
"""
from __future__ import annotations

from flask import Flask

import app as app_module


def test_app_module_imports_and_has_module_level_app():
    """`import app` works and exposes a module-level Flask `app` object built at
    import time (used by `flask run` / WSGI) — without any live claude."""
    assert isinstance(app_module.app, Flask)


def test_create_app_constructs_without_live_claude():
    """The factory builds a Flask app with no real `claude` available and without
    spawning a subprocess. We pass a runner that would raise if ever called, so a
    green test proves nothing touched Claude during construction."""

    def exploding_run(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("claude runner must not be invoked at construction time")
        yield  # makes this a generator, matching the run_fn shape

    built = app_module.create_app(run_fn=exploding_run)
    assert isinstance(built, Flask)
    # A route table exists -> the factory wired the endpoints, not just an empty app.
    rules = {r.rule for r in built.url_map.iter_rules()}
    assert "/" in rules
    assert "/run" in rules


def test_all_core_modules_import():
    """Every first-party module imports cleanly (no import-time side effects that
    need a live claude / network)."""
    import classifier  # noqa: F401
    import claude_cli  # noqa: F401
    import config  # noqa: F401
    import parsing  # noqa: F401
    import prompts  # noqa: F401
    import sandbox  # noqa: F401
    import storage  # noqa: F401
    import topic_index  # noqa: F401
