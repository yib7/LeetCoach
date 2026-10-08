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


# --- B8: LEETCOACH_NO_DOTENV must actually gate the dotenv load ------------

def test_maybe_load_dotenv_skips_when_no_dotenv_flag_set(tmp_path, monkeypatch):
    import os

    envfile = tmp_path / ".env"
    envfile.write_text("LEETCOACH_SENTINEL_B8=from_dotenv\n", encoding="utf-8")
    monkeypatch.delenv("LEETCOACH_SENTINEL_B8", raising=False)
    monkeypatch.setenv("LEETCOACH_NO_DOTENV", "1")

    app_module._maybe_load_dotenv(envfile)

    assert "LEETCOACH_SENTINEL_B8" not in os.environ


def test_maybe_load_dotenv_loads_when_flag_unset(tmp_path, monkeypatch):
    import os

    envfile = tmp_path / ".env"
    envfile.write_text("LEETCOACH_SENTINEL_B8=from_dotenv\n", encoding="utf-8")
    monkeypatch.delenv("LEETCOACH_SENTINEL_B8", raising=False)
    monkeypatch.delenv("LEETCOACH_NO_DOTENV", raising=False)

    app_module._maybe_load_dotenv(envfile)

    assert os.environ.get("LEETCOACH_SENTINEL_B8") == "from_dotenv"
    monkeypatch.delenv("LEETCOACH_SENTINEL_B8", raising=False)


def test_suite_env_isolation_sets_no_dotenv_and_private_output_dir():
    """The autouse fixture in tests/conftest.py (B8) must have already isolated
    this test's environment by the time it runs."""
    import os

    assert os.environ.get("LEETCOACH_NO_DOTENV") == "1"
    assert "leetcoach-output" in os.environ.get("LEETCOACH_OUTPUT_DIR", "")


# --- B12: a UTF-16 / BOM .env must not crash the boot ------------------------

def test_maybe_load_dotenv_reads_utf16_and_bom_files(tmp_path, monkeypatch):
    import os

    monkeypatch.delenv("LEETCOACH_NO_DOTENV", raising=False)
    for raw in ("LEETCOACH_SENTINEL_B12=ok\n".encode("utf-16"),
                b"\xef\xbb\xbfLEETCOACH_SENTINEL_B12=ok\n"):
        envfile = tmp_path / ".env"
        envfile.write_bytes(raw)
        monkeypatch.delenv("LEETCOACH_SENTINEL_B12", raising=False)
        app_module._maybe_load_dotenv(envfile)
        assert os.environ.get("LEETCOACH_SENTINEL_B12") == "ok"
    monkeypatch.delenv("LEETCOACH_SENTINEL_B12", raising=False)


def test_maybe_load_dotenv_survives_an_undecodable_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LEETCOACH_NO_DOTENV", raising=False)
    envfile = tmp_path / ".env"
    envfile.write_bytes(b"A=\xff\xfe\xfa\n")
    app_module._maybe_load_dotenv(envfile)  # must not raise
