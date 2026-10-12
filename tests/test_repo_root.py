"""The user's own files stay at the repo root after the code moved into the
``leetcoach/`` package (Phase 5): the default ``output/`` study library, the
``.env`` the app loads and the model picker writes, and the sandbox's guard
around that ``.env``. An existing checkout's library and settings keep working
with no migration, so every one of these must resolve from ``config.REPO_ROOT``
(the folder holding ``leetcoach/``), never from the package directory.

The expected root is computed here from this file's own location (``tests/``
sits at the repo root), independently of the code under test.
"""
from __future__ import annotations

import os
from pathlib import Path

from leetcoach import app as app_module
from leetcoach import config, sandbox

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_repo_root_is_the_folder_holding_the_package():
    assert config.REPO_ROOT == REPO_ROOT
    assert config.PACKAGE_DIR == REPO_ROOT / "leetcoach"
    assert (config.REPO_ROOT / "app.py").is_file()  # the `python app.py` entry point


def test_default_output_dir_is_the_repo_root_output(monkeypatch):
    monkeypatch.delenv("LEETCOACH_OUTPUT_DIR", raising=False)
    assert config.DEFAULT_OUTPUT_DIR == REPO_ROOT / "output"
    assert config.output_dir() == REPO_ROOT / "output"


def test_default_topic_index_lives_in_the_repo_root_output(monkeypatch):
    monkeypatch.delenv("LEETCOACH_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("LEETCOACH_TOPIC_INDEX", raising=False)
    assert config.topic_index_path() == REPO_ROOT / "output" / "topic_index.json"


def test_dotenv_is_the_repo_root_env(monkeypatch):
    assert config.DOTENV_FILE == REPO_ROOT / ".env"
    # The model picker's default write target, when no override is set.
    monkeypatch.delenv("LEETCOACH_DOTENV_PATH", raising=False)
    application = app_module.create_app(run_fn=lambda *a, **k: iter(()),
                                        auth_probe=lambda: None)
    assert application.config["DOTENV_PATH"] == str(REPO_ROOT / ".env")


def test_sandbox_guards_the_repo_root_env():
    paths = {os.path.normcase(os.path.abspath(p)) for p in sandbox._secret_paths()}
    assert os.path.normcase(str(REPO_ROOT / ".env")) in paths


def test_page_and_assets_ship_inside_the_package():
    assert app_module.TEMPLATES == REPO_ROOT / "leetcoach" / "templates"
    assert app_module.STATIC == REPO_ROOT / "leetcoach" / "static"
    assert (app_module.TEMPLATES / "index.html").is_file()
    assert (app_module.STATIC / "app.js").is_file()
