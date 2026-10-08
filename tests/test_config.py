"""Tests for the config helpers behind the in-app model picker.

Covered:

* ``model_alias`` maps the configured model id to one of the picker's aliases
  (or "" when none match), so the picker highlights the right button;
* ``upsert_env_var`` sets ``KEY=value`` in a dotenv file in place — replacing an
  existing assignment, appending a missing one, creating the file when absent,
  and never disturbing other lines/comments.
"""
from __future__ import annotations

import config

# --- model_alias ----------------------------------------------------------

def test_model_alias_maps_default_opus_id(monkeypatch):
    monkeypatch.delenv("LEETCOACH_MODEL", raising=False)
    # the default is the generic `opus` alias -> the picker's "opus"
    assert config.DEFAULT_MODEL == "opus"
    assert config.model() == "opus"
    assert config.model_alias() == "opus"


def test_model_alias_maps_latest_pinned_ids(monkeypatch):
    for model_id, alias in (
        ("claude-fable-5-1", "fable"),
        ("claude-opus-5-5", "opus"),
        ("claude-sonnet-5-5", "sonnet"),
        ("claude-haiku-5-5", "haiku"),
    ):
        monkeypatch.setenv("LEETCOACH_MODEL", model_id)
        assert config.model_alias() == alias


def test_aliases_and_display_ids_cover_the_same_models():
    assert config.ALLOWED_MODEL_ALIASES == ("fable", "opus", "sonnet", "haiku")
    assert set(config.LATEST_MODEL_IDS) == set(config.ALLOWED_MODEL_ALIASES)
    assert config.classifier_model() == "haiku"
    assert config.quick_ask_model() == "haiku"


def test_model_alias_matches_each_alias(monkeypatch):
    for alias in ("fable", "opus", "sonnet", "haiku"):
        monkeypatch.setenv("LEETCOACH_MODEL", alias)
        assert config.model_alias() == alias


def test_model_alias_matches_pinned_id(monkeypatch):
    monkeypatch.setenv("LEETCOACH_MODEL", "claude-sonnet-5")
    assert config.model_alias() == "sonnet"


def test_model_alias_empty_for_unknown_model(monkeypatch):
    monkeypatch.setenv("LEETCOACH_MODEL", "some-other-model")
    assert config.model_alias() == ""


# --- upsert_env_var -------------------------------------------------------

def test_upsert_creates_file_when_absent(tmp_path):
    env = tmp_path / ".env"
    assert not env.exists()
    config.upsert_env_var(env, "LEETCOACH_MODEL", "sonnet")
    assert env.read_text(encoding="utf-8") == "LEETCOACH_MODEL=sonnet\n"


def test_upsert_appends_when_key_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# my config\nLEETCOACH_OUTPUT_DIR=lib\n", encoding="utf-8")
    config.upsert_env_var(env, "LEETCOACH_MODEL", "opus")
    text = env.read_text(encoding="utf-8")
    # the existing lines are untouched, the new key is appended
    assert "# my config" in text
    assert "LEETCOACH_OUTPUT_DIR=lib" in text
    assert text.rstrip().endswith("LEETCOACH_MODEL=opus")


def test_upsert_replaces_existing_key_in_place(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# top\nLEETCOACH_MODEL=opus\nLEETCOACH_RUN_TIMEOUT=600\n",
        encoding="utf-8",
    )
    config.upsert_env_var(env, "LEETCOACH_MODEL", "haiku")
    lines = env.read_text(encoding="utf-8").splitlines()
    # replaced exactly once, in its original position, others intact
    assert lines == ["# top", "LEETCOACH_MODEL=haiku", "LEETCOACH_RUN_TIMEOUT=600"]
    assert lines.count("LEETCOACH_MODEL=haiku") == 1


def test_upsert_ignores_commented_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("#LEETCOACH_MODEL=opus\n", encoding="utf-8")
    config.upsert_env_var(env, "LEETCOACH_MODEL", "sonnet")
    text = env.read_text(encoding="utf-8")
    # the comment is preserved and a real assignment is appended
    assert "#LEETCOACH_MODEL=opus" in text
    assert "LEETCOACH_MODEL=sonnet" in text


def test_upsert_tolerates_export_prefix(tmp_path):
    env = tmp_path / ".env"
    env.write_text("export LEETCOACH_MODEL=opus\n", encoding="utf-8")
    config.upsert_env_var(env, "LEETCOACH_MODEL", "haiku")
    lines = env.read_text(encoding="utf-8").splitlines()
    # the export-prefixed assignment is recognised and replaced (not duplicated)
    assert lines == ["LEETCOACH_MODEL=haiku"]
