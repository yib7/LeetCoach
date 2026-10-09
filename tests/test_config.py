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


# --- B12: robust .env upsert ------------------------------------------------

import pytest  # noqa: E402


def test_upsert_handles_utf8_bom_without_duplicating_the_first_key(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfLEETCOACH_MODEL=opus\nOTHER=1\n")
    config.upsert_env_var(env, "LEETCOACH_MODEL", "haiku")
    raw = env.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # written back as plain UTF-8
    assert raw.decode("utf-8").splitlines() == ["LEETCOACH_MODEL=haiku", "OTHER=1"]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_upsert_reads_utf16_files(tmp_path, encoding):
    env = tmp_path / ".env"
    env.write_bytes("# picked\nLEETCOACH_MODEL=opus\n".encode(encoding))
    config.upsert_env_var(env, "LEETCOACH_MODEL", "sonnet")
    # rewritten as UTF-8 so python-dotenv (UTF-8) can read it at boot
    assert env.read_bytes().decode("utf-8").splitlines() == ["# picked", "LEETCOACH_MODEL=sonnet"]


def test_upsert_keeps_the_first_assignment_and_drops_later_duplicates(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "LEETCOACH_MODEL=opus\nX=1\nLEETCOACH_MODEL=haiku\nexport LEETCOACH_MODEL=sonnet\n",
        encoding="utf-8",
    )
    config.upsert_env_var(env, "LEETCOACH_MODEL", "fable")
    lines = env.read_text(encoding="utf-8").splitlines()
    # SP4 review M4: the first occurrence is updated in place and the later
    # duplicates are removed, so no stale assignment can win (dotenv is last-wins)
    assert lines == ["LEETCOACH_MODEL=fable", "X=1"]


def test_upsert_aborts_on_read_error_and_leaves_the_file_alone(tmp_path, monkeypatch):
    from pathlib import Path

    env = tmp_path / ".env"
    env.write_text("KEEP=me\n", encoding="utf-8")
    real_read_bytes = Path.read_bytes

    def locked(self):
        if self == env:
            raise PermissionError(13, "locked")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", locked)
    with pytest.raises(OSError):
        config.upsert_env_var(env, "LEETCOACH_MODEL", "opus")
    monkeypatch.undo()
    assert env.read_text(encoding="utf-8") == "KEEP=me\n"  # not wiped


def test_upsert_aborts_on_undecodable_file(tmp_path):
    env = tmp_path / ".env"
    original = b"KEEP=\xff\xfe\xfa broken\n"
    env.write_bytes(original)
    with pytest.raises(ValueError):
        config.upsert_env_var(env, "LEETCOACH_MODEL", "opus")
    assert env.read_bytes() == original


def test_upsert_writes_atomically(tmp_path, monkeypatch):
    import fsutil

    seen = []
    real = fsutil.atomic_write_text
    monkeypatch.setattr(
        fsutil, "atomic_write_text",
        lambda p, t, **kw: (seen.append(str(p)), real(p, t, **kw))[1],
    )
    env = tmp_path / ".env"
    config.upsert_env_var(env, "LEETCOACH_MODEL", "opus")
    assert seen == [str(env)]


@pytest.mark.parametrize("raw,expected", [
    (b"\xef\xbb\xbfA=1\n", "A=1\n"),
    ("A=1\n".encode("utf-16"), "A=1\n"),
    (b"A=caf\xc3\xa9\n", "A=café\n"),
])
def test_read_env_text_decodes_bom_and_utf16(tmp_path, raw, expected):
    env = tmp_path / ".env"
    env.write_bytes(raw)
    assert config.read_env_text(env).replace("\r\n", "\n") == expected


def test_read_env_text_missing_file_is_empty(tmp_path):
    assert config.read_env_text(tmp_path / "nope.env") == ""


def _text(path):
    """The file's text with the platform line ending folded to a bare LF."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


# --- SP4 review M3: line splitting and quoted multi-line values -----------------

def test_upsert_splits_only_on_newlines(tmp_path):
    env = tmp_path / ".env"
    # U+2028, U+0085 and form feed are NOT line breaks in a dotenv file
    other = "NOTE=a\u2028b\x85c\x0cd"
    env.write_bytes(f"{other}\r\nLEETCOACH_MODEL=opus\r\n".encode())
    config.upsert_env_var(env, "LEETCOACH_MODEL", "haiku")
    text = _text(env)
    assert text.split("\n") == [other, "LEETCOACH_MODEL=haiku", ""]


def test_upsert_leaves_a_key_inside_a_quoted_multiline_value_alone(tmp_path):
    env = tmp_path / ".env"
    body = (
        'BLURB="first line\n'
        "LEETCOACH_MODEL=inside-the-quote\n"
        'last line"\n'
        "LEETCOACH_MODEL=opus\n"
        "SINGLE='LEETCOACH_MODEL=x'\n"
    )
    env.write_bytes(body.encode("utf-8"))
    config.upsert_env_var(env, "LEETCOACH_MODEL", "haiku")
    assert _text(env) == body.replace(
        "LEETCOACH_MODEL=opus", "LEETCOACH_MODEL=haiku"
    )


def test_upsert_drops_a_later_duplicate_with_a_multiline_value_entirely(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b'LEETCOACH_MODEL=opus\nLEETCOACH_MODEL="multi\nline"\nAFTER=1\n')
    config.upsert_env_var(env, "LEETCOACH_MODEL", "haiku")
    assert _text(env) == "LEETCOACH_MODEL=haiku\nAFTER=1\n"


def test_upsert_escaped_quote_does_not_end_a_double_quoted_value(tmp_path):
    env = tmp_path / ".env"
    body = 'A="say \\"hi\\"\nLEETCOACH_MODEL=inside"\nLEETCOACH_MODEL=opus\n'
    env.write_bytes(body.encode("utf-8"))
    config.upsert_env_var(env, "LEETCOACH_MODEL", "haiku")
    assert _text(env) == body.replace(
        "LEETCOACH_MODEL=opus", "LEETCOACH_MODEL=haiku"
    )


# --- Cycle 11 review: an unclosed quote must not swallow the rest of the file ----

def test_upsert_unclosed_quote_on_the_target_keeps_every_following_line(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b'LEETCOACH_MODEL="sonnet\nOTHER=1\n# keep me\nZ=2\n')
    config.upsert_env_var(env, "LEETCOACH_MODEL", "haiku")
    assert _text(env) == "LEETCOACH_MODEL=haiku\nOTHER=1\n# keep me\nZ=2\n"


def test_upsert_unclosed_quote_on_a_later_duplicate_keeps_following_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"LEETCOACH_MODEL=opus\nA=1\nLEETCOACH_MODEL='haiku\nB=2\nC=3\n")
    config.upsert_env_var(env, "LEETCOACH_MODEL", "haiku")
    assert _text(env) == "LEETCOACH_MODEL=haiku\nA=1\nB=2\nC=3\n"


def test_unclosed_quote_on_another_key_does_not_hide_a_later_assignment(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b'NOTE="never closed\nLEETCOACH_MODEL=opus\nZ=2\n')
    config.upsert_env_var(env, "LEETCOACH_MODEL", "haiku")
    # python-dotenv skips the broken NOTE line and reads the next ones normally,
    # so the real assignment is updated in place (not duplicated at the end)
    assert _text(env) == 'NOTE="never closed\nLEETCOACH_MODEL=haiku\nZ=2\n'


# Each case is a list of (line, is_target): ``is_target`` marks the lines that
# belong to a LEETCOACH_MODEL assignment (including the continuation lines of a
# CLOSED quoted value). Every other line must survive an upsert byte-for-byte.
_T = "LEETCOACH_MODEL"
_TRICKY_ENVS = [
    [(f'{_T}="sonnet', True), ("OTHER=1", False), ("# keep me", False), ("Z=2", False)],
    [(f"{_T}=opus", True), ("A=1", False), (f"{_T}='haiku", True), ("B=2", False),
     ("C=3", False)],
    [("", False), ("# head", False), (f"{_T}='x", True), ("", False),
     ("export Q=\"unclosed", False), (f"{_T}=y", True), ("W=1", False)],
    [('A="multi', False), (f"{_T}=inside", False), ('end"', False), (f"{_T}=opus", True),
     ("B='also unclosed", False), ("", False), ("# tail", False)],
    [(f'{_T}="closed', True), ('multi"', True), ("KEEP=1", False), (f'{_T}="open', True),
     ("K2=2", False)],
    [('A="say \\"hi\\"', False), ("still a", False), ('done"', False), (f"{_T}=\"\\\"", True),
     ("B=1", False), ("# c", False)],
    [("# only comments", False), ("", False), ("X='never", False), ("Y=\"never", False)],
]


@pytest.mark.parametrize("case", _TRICKY_ENVS)
def test_upsert_preserves_every_non_target_line_byte_for_byte(tmp_path, case):
    env = tmp_path / ".env"
    env.write_bytes(("\n".join(line for line, _ in case) + "\n").encode("utf-8"))
    config.upsert_env_var(env, _T, "haiku")
    expected: list[str] = []
    placed = False
    for line, is_target in case:
        if not is_target:
            expected.append(line)
        elif not placed:
            expected.append(f"{_T}=haiku")
            placed = True
    if not placed:
        expected.append(f"{_T}=haiku")
    assert _text(env).split("\n") == [*expected, ""]


# --- SP4 review I1: a per-run alias vs a pinned id ------------------------------

def test_resolve_run_model_keeps_a_pinned_id_for_its_own_alias(monkeypatch):
    monkeypatch.setenv("LEETCOACH_MODEL", "claude-sonnet-4-5")
    assert config.resolve_run_model("sonnet") == "claude-sonnet-4-5"
    assert config.resolve_run_model("haiku") == "haiku"
    assert config.resolve_run_model("") == ""


# --- 3A C10: an empty (or blank) knob means "unset" ---------------------------

@pytest.mark.parametrize("value", ["", "   ", "\t"])
@pytest.mark.parametrize("env_var, getter, default", [
    ("LEETCOACH_MODEL", config.model, config.DEFAULT_MODEL),
    ("LEETCOACH_CLASSIFIER_MODEL", config.classifier_model, config.DEFAULT_CLASSIFIER_MODEL),
    ("LEETCOACH_QUICK_ASK_MODEL", config.quick_ask_model, config.DEFAULT_QUICK_ASK_MODEL),
    ("LEETCOACH_CLAUDE_BIN", config.claude_bin, config.DEFAULT_CLAUDE_BIN),
])
def test_blank_string_knobs_fall_back_to_their_default(monkeypatch, env_var, getter, default, value):
    # `LEETCOACH_MODEL=` in .env used to reach argv as `--model ""`.
    monkeypatch.setenv(env_var, value)
    assert getter() == default


def test_string_knobs_are_stripped(monkeypatch):
    monkeypatch.setenv("LEETCOACH_MODEL", "  sonnet  ")
    assert config.model() == "sonnet"
