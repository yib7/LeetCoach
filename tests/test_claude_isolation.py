"""A7: every `claude -p` call runs isolated from the developer's Claude Code setup.

Covers the argv the wrapper builds (flag-gated on a cached ``claude --help``
probe), the neutral working directory, the tutor persona moving into
``--system-prompt``, the session-persistence rules, and ``session_id`` capture.

No real ``claude`` is spawned: argv tests inject a recording runner, the help
probe is fed canned text by the autouse fixture (tests/conftest.py) or an
injected ``probe=``, and the few real-subprocess tests use this Python
interpreter as a harmless stand-in.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from _helpers import FAKE_CLAUDE_HELP

import classifier
import claude_cli
import config
import prompts

ALL_FLAGS = claude_cli.parse_help_flags(FAKE_CLAUDE_HELP)
# Captured at import, before the suite-wide autouse fixture swaps in canned help.
REAL_PROBE_HELP_TEXT = claude_cli._probe_help_text


def recording_runner(lines=None):
    calls = []

    def runner(argv, stdin_text, **kwargs):
        calls.append({"argv": list(argv), "stdin": stdin_text, **kwargs})
        yield from (lines if lines is not None else [
            json.dumps({"type": "stream_event", "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "OK"}}}),
            json.dumps({"type": "result", "subtype": "success", "result": "OK"}),
        ])

    return runner, calls


def _argv(**kwargs):
    runner, calls = recording_runner()
    list(claude_cli.run("hi", runner=runner, **kwargs))
    return calls[0]


# --- help-probe parsing + gating -------------------------------------------

def test_parse_help_flags_finds_long_options():
    flags = claude_cli.parse_help_flags(FAKE_CLAUDE_HELP)
    for flag in ("--safe-mode", "--tools", "--strict-mcp-config", "--system-prompt",
                 "--no-session-persistence", "--model", "--print"):
        assert flag in flags
    assert claude_cli.parse_help_flags("") == frozenset()


def test_parse_help_flags_ignores_flags_only_mentioned_in_descriptions():
    # SP2 M4: a flag the CLI does NOT support, named only inside another
    # option's description, must not be taken as supported.
    text = (
        "Usage: claude [options]\n\n"
        "Options:\n"
        "  -p, --print                 Print response and exit (see also --safe-mode)\n"
        "  --model <model>             Model; replaces the old --system-prompt\n"
        "  --strict-mcp-config         Only use MCP servers from --mcp-config\n"
        "  --no-session-persistence    Disable session persistence\n"
    )
    flags = claude_cli.parse_help_flags(text)
    assert flags == frozenset(
        {"--print", "--model", "--strict-mcp-config", "--no-session-persistence"}
    )


def test_parse_help_flags_ignores_wrapped_description_continuation_lines():
    # SP2 M4 follow-up: a wrapped description continuation line is still
    # indented, and if it happens to start with something that LOOKS like a
    # flag (e.g. "--config" mentioned mid-sentence), the old unbounded
    # `^\s+` indent must not mistake it for a real option-column entry. Real
    # option lines sit at a shallow indent (2 spaces in `claude --help`);
    # continuation lines are indented far past the option column.
    text = (
        "Usage: claude [options]\n\n"
        "Options:\n"
        "  --model <model>              Model name; falls back to whatever\n"
        "                                --config sets by default when omitted\n"
        "  --safe-mode                   Start with customizations disabled\n"
    )
    flags = claude_cli.parse_help_flags(text)
    assert flags == frozenset({"--model", "--safe-mode"})
    assert "--config" not in flags


def test_help_probe_runs_in_the_neutral_cwd(monkeypatch, tmp_path):
    seen = {}

    def fake_bounded(argv, *, timeout, cwd=None):
        seen["argv"], seen["cwd"] = argv, cwd
        return 0, FAKE_CLAUDE_HELP

    monkeypatch.setattr(claude_cli, "_run_bounded", fake_bounded)
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", str(tmp_path / "neutral"))
    assert REAL_PROBE_HELP_TEXT(["no-such-claude", "--help"]) == FAKE_CLAUDE_HELP
    assert seen["cwd"] == str(tmp_path / "neutral")
    assert Path(seen["cwd"]).is_dir()


def test_isolation_flags_present_when_cli_lists_them():
    argv = _argv(flags=ALL_FLAGS, system_prompt="Persona text.")["argv"]
    assert "--safe-mode" in argv
    assert "--strict-mcp-config" in argv
    i = argv.index("--tools")
    assert argv[i + 1] == ""  # "" disables every built-in tool
    # the variadic --tools must be followed by another flag, never a bare value
    assert argv[i + 2].startswith("--")
    j = argv.index("--system-prompt")
    assert argv[j + 1] == "Persona text."


def test_bare_is_never_passed_even_if_listed():
    # --bare never reads OAuth -> would break subscription auth.
    argv = _argv(flags=ALL_FLAGS | {"--bare"})["argv"]
    assert "--bare" not in argv


def test_unlisted_flags_are_not_passed_older_cli():
    call = _argv(flags=frozenset(), system_prompt="Persona text.")
    argv = call["argv"]
    for flag in ("--safe-mode", "--tools", "--strict-mcp-config", "--system-prompt",
                 "--no-session-persistence"):
        assert flag not in argv
    # without --system-prompt the persona still reaches Claude, on stdin
    assert call["stdin"].startswith("Persona text.")
    assert call["stdin"].rstrip().endswith("hi")


def test_system_prompt_not_duplicated_into_stdin_when_flag_supported():
    call = _argv(flags=ALL_FLAGS, system_prompt="Persona text.")
    assert call["stdin"] == "hi"


def test_default_system_prompt_is_used_when_none_given():
    argv = _argv(flags=ALL_FLAGS)["argv"]
    j = argv.index("--system-prompt")
    assert argv[j + 1] == claude_cli.DEFAULT_SYSTEM_PROMPT


def test_persistence_rules():
    study = _argv(flags=ALL_FLAGS, persist_session=True)["argv"]
    utility = _argv(flags=ALL_FLAGS, persist_session=False)["argv"]
    assert "--no-session-persistence" not in study
    assert "--no-session-persistence" in utility
    # default is the safe one: no persisted session
    assert "--no-session-persistence" in _argv(flags=ALL_FLAGS)["argv"]


def test_flags_default_to_the_cached_help_probe(monkeypatch):
    calls = []

    def probe(argv):
        calls.append(list(argv))
        return FAKE_CLAUDE_HELP

    monkeypatch.setattr(claude_cli, "_probe_help_text", probe)
    claude_cli.clear_flag_cache()
    a = _argv()["argv"]
    b = _argv()["argv"]
    assert "--safe-mode" in a and "--safe-mode" in b
    assert len(calls) == 1, "help probe must be cached, not run per call"
    assert calls[0][-1] == "--help"


def test_failed_help_probe_degrades_to_no_optional_flags(monkeypatch):
    def boom(argv):
        raise OSError("probe exploded")

    monkeypatch.setattr(claude_cli, "_probe_help_text", boom)
    claude_cli.clear_flag_cache()
    assert claude_cli.cli_supported_flags() == frozenset()
    argv = _argv()["argv"]  # the run itself still works
    assert "--safe-mode" not in argv and "-p" in argv


def test_failed_probe_is_retried_after_short_ttl(monkeypatch):
    results = [None, FAKE_CLAUDE_HELP]
    monkeypatch.setattr(claude_cli, "_probe_help_text", lambda argv: results.pop(0))
    clock = [1000.0]
    monkeypatch.setattr(claude_cli, "_monotonic", lambda: clock[0])
    claude_cli.clear_flag_cache()
    assert claude_cli.cli_supported_flags() == frozenset()
    assert claude_cli.cli_supported_flags() == frozenset()  # still cached
    clock[0] += claude_cli.FLAG_PROBE_FAILURE_TTL + 1
    assert "--safe-mode" in claude_cli.cli_supported_flags()


def _probe_warnings(caplog):
    return [r for r in caplog.records
            if r.name == "claude_cli" and r.levelno == logging.WARNING
            and "--help" in r.getMessage()]


def test_failed_help_probe_logs_a_warning_naming_the_exception(monkeypatch, caplog):
    # 3A C2: degrading to no isolation flags must not be silent.
    def boom(argv):
        raise OSError("probe exploded")

    monkeypatch.setattr(claude_cli, "_probe_help_text", boom)
    claude_cli.clear_flag_cache()
    with caplog.at_level(logging.WARNING, logger="claude_cli"):
        assert claude_cli.cli_supported_flags() == frozenset()
    warning, = _probe_warnings(caplog)
    assert "OSError" in warning.getMessage()
    assert "--safe-mode" in warning.getMessage()


def test_nonzero_help_probe_logs_the_exit_code(monkeypatch, caplog):
    monkeypatch.setattr(claude_cli, "_probe_help_text", REAL_PROBE_HELP_TEXT)
    monkeypatch.setattr(claude_cli, "_run_bounded", lambda argv, *, timeout, cwd=None: (3, ""))
    claude_cli.clear_flag_cache()
    with caplog.at_level(logging.WARNING, logger="claude_cli"):
        assert claude_cli.cli_supported_flags() == frozenset()
    warning, = _probe_warnings(caplog)
    assert "exit code 3" in warning.getMessage()


def test_help_probe_timeout_is_retried_once_before_degrading(monkeypatch, caplog):
    calls = []

    def slow_then_ok(argv):
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, claude_cli.FLAG_PROBE_TIMEOUT)
        return FAKE_CLAUDE_HELP

    monkeypatch.setattr(claude_cli, "_probe_help_text", slow_then_ok)
    claude_cli.clear_flag_cache()
    with caplog.at_level(logging.WARNING, logger="claude_cli"):
        assert "--safe-mode" in claude_cli.cli_supported_flags()
    assert len(calls) == 2
    assert not _probe_warnings(caplog)


def test_help_probe_timing_out_twice_degrades_with_a_warning(monkeypatch, caplog):
    calls = []

    def always_slow(argv):
        calls.append(1)
        raise subprocess.TimeoutExpired(argv, claude_cli.FLAG_PROBE_TIMEOUT)

    monkeypatch.setattr(claude_cli, "_probe_help_text", always_slow)
    claude_cli.clear_flag_cache()
    with caplog.at_level(logging.WARNING, logger="claude_cli"):
        assert claude_cli.cli_supported_flags() == frozenset()
    assert len(calls) == 2, "a timeout is retried exactly once"
    warning, = _probe_warnings(caplog)
    assert "timed out" in warning.getMessage()


def test_successful_help_probe_logs_nothing(caplog):
    claude_cli.clear_flag_cache()
    with caplog.at_level(logging.WARNING, logger="claude_cli"):
        assert "--safe-mode" in claude_cli.cli_supported_flags()
    assert not _probe_warnings(caplog)


def test_real_help_probe_is_bounded_and_decodes_utf8(monkeypatch):
    # The real probe helper runs through the bounded runner (timeout + tree
    # kill). Point it at a Python stand-in that prints UTF-8 help text.
    script = (
        "import sys\n"
        "sys.stdout.buffer.write('  --safe-mode   caf\\u00e9 \\u0101\\n'.encode('utf-8'))\n"
    )
    text = claude_cli._run_bounded([sys.executable, "-c", script], timeout=20)[1]
    assert "--safe-mode" in text and "\u0101" in text


# --- API-billing env vars are withheld (3A C1) --------------------------------

# Every variable that would switch Claude Code from the subscription login to
# API-credit billing. Obvious placeholders only - never a real-looking key.
_BILLING_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)
_PLACEHOLDER = "test-placeholder"
_ENV_DUMP = "import json, os, sys\nsys.stdin.read()\nprint(json.dumps(dict(os.environ)))\n"


def _set_billing_env(monkeypatch):
    for name in _BILLING_VARS:
        monkeypatch.setenv(name, _PLACEHOLDER)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9/" + _PLACEHOLDER)
    monkeypatch.setattr(claude_cli, "_withheld_logged", False)


def _assert_child_env_scrubbed(env):
    for name in _BILLING_VARS:
        assert name not in env, f"{name} leaked into the claude child"
    # everything else is inherited untouched
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9/" + _PLACEHOLDER
    assert env.get("PATH")


def test_real_runner_withholds_api_billing_env_vars(monkeypatch):
    _set_billing_env(monkeypatch)
    lines = list(claude_cli._real_runner([sys.executable, "-c", _ENV_DUMP], ""))
    _assert_child_env_scrubbed(json.loads(lines[-1]))
    # the app's own environment is not modified
    assert all(os.environ[name] == _PLACEHOLDER for name in _BILLING_VARS)


def test_bounded_probe_runner_withholds_api_billing_env_vars(monkeypatch):
    # _run_bounded serves both the `--help` and the `auth status` probes.
    _set_billing_env(monkeypatch)
    returncode, out = claude_cli._run_bounded([sys.executable, "-c", _ENV_DUMP], timeout=20)
    assert returncode == 0
    _assert_child_env_scrubbed(json.loads(out))


def test_withheld_env_vars_are_logged_once_by_name_only(monkeypatch, caplog):
    _set_billing_env(monkeypatch)
    with caplog.at_level(logging.INFO, logger="claude_cli"):
        claude_cli.child_env()
        claude_cli.child_env()
    records = [r for r in caplog.records if r.name == "claude_cli"]
    assert len(records) == 1, "the withheld-vars notice must be logged once per process"
    message = records[0].getMessage()
    for name in _BILLING_VARS:
        assert name in message
    assert _PLACEHOLDER not in message, "a withheld value must never be logged"


def test_child_env_is_silent_when_nothing_is_withheld(monkeypatch, caplog):
    for name in _BILLING_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(claude_cli, "_withheld_logged", False)
    with caplog.at_level(logging.INFO, logger="claude_cli"):
        env = claude_cli.child_env()
    assert env.get("PATH")
    assert not [r for r in caplog.records if r.name == "claude_cli"]


# --- neutral cwd ------------------------------------------------------------

def test_run_uses_neutral_cwd_and_creates_it(tmp_path, monkeypatch):
    target = tmp_path / "neutral" / "dir"
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", str(target))
    call = _argv(flags=ALL_FLAGS)
    assert Path(call["cwd"]) == target
    assert target.is_dir()


def test_claude_cwd_defaults_per_platform():
    win = config.default_claude_cwd(
        os_name="nt", env={"LOCALAPPDATA": r"C:\Users\u\AppData\Local"},
        home=Path(r"C:\Users\u"),
    )
    assert win == Path(r"C:\Users\u\AppData\Local") / "LeetCoach" / "claude-cwd"
    posix = config.default_claude_cwd(os_name="posix", env={}, home=Path("/home/u"))
    assert posix == Path("/home/u") / ".local" / "share" / "leetcoach" / "claude-cwd"


def test_claude_cwd_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", str(tmp_path / "x"))
    assert config.claude_cwd() == tmp_path / "x"


_HOME_VARS = ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")


def _no_home(monkeypatch):
    """No way to resolve a home directory: Path.home() raises, as it does
    with every home variable unset."""
    for name in _HOME_VARS:
        monkeypatch.delenv(name, raising=False)

    def no_home(*args, **kwargs):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)


def test_default_cwd_with_localappdata_never_needs_home(monkeypatch):
    # 3A C5: the Windows branch only falls back to ~ when LOCALAPPDATA is unset.
    _no_home(monkeypatch)
    got = config.default_claude_cwd(os_name="nt", env={"LOCALAPPDATA": r"C:\LAD"})
    assert got == Path(r"C:\LAD") / "LeetCoach" / "claude-cwd"


def test_ensure_cwd_survives_an_unresolvable_home(tmp_path, monkeypatch):
    # 3A C5: "a run must never fail over this" - even with no home at all.
    _no_home(monkeypatch)
    monkeypatch.delenv("LEETCOACH_CLAUDE_CWD", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    got = Path(claude_cli.ensure_claude_cwd())
    assert got.is_dir()
    assert tmp_path in got.parents


def test_claude_cwd_override_expands_user_and_resolves(monkeypatch, tmp_path):
    # 3A C7: `~/lc` from .env must not become a literal "~" folder.
    for name in _HOME_VARS:
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", "~/lc")
    got = config.claude_cwd()
    assert got == (tmp_path / "lc").resolve()
    assert got.is_absolute()


def test_claude_cwd_override_with_unresolvable_home_is_used_verbatim(monkeypatch):
    _no_home(monkeypatch)
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", "~/lc")
    assert config.claude_cwd().name == "lc"  # never raises


def test_temp_fallback_name_is_per_user(tmp_path, monkeypatch):
    # 3A C6: never one shared name in a multi-user temp dir.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    name = claude_cli._temp_cwd_candidate().name
    assert name.startswith("leetcoach-claude-cwd-")
    assert name != "leetcoach-claude-cwd-"
    if os.name != "nt":
        assert name.endswith(f"-{os.getuid()}")


def _block_every_candidate(tmp_path, monkeypatch):
    blocker = tmp_path / "a_file"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", str(blocker / "sub"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    claude_cli._temp_cwd_candidate().write_text("squatter", encoding="utf-8")
    monkeypatch.setattr(claude_cli, "_mkdtemp_cwd", None)


def test_last_resort_temp_dir_is_created_once_per_process(tmp_path, monkeypatch):
    # 3A C6: a fresh mkdtemp on every call leaked dirs and split the
    # --resume session bucket between calls.
    _block_every_candidate(tmp_path, monkeypatch)
    first = claude_cli.ensure_claude_cwd()
    second = claude_cli.ensure_claude_cwd()
    assert first == second
    assert Path(first).is_dir() and tmp_path in Path(first).parents


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership check")
def test_temp_fallback_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    blocker = tmp_path / "a_file"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", str(blocker / "sub"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(claude_cli, "_mkdtemp_cwd", None)
    # We pretend to be uid+1, so the dir we create here under the real uid
    # is "someone else's" squat on our per-user name.
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    planted = claude_cli._temp_cwd_candidate()
    assert planted.name.endswith(f"-{real_uid + 1}")
    planted.mkdir()
    (planted / "CLAUDE.md").write_text("planted", encoding="utf-8")
    got = Path(claude_cli.ensure_claude_cwd())
    assert got != planted and got.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_temp_fallback_is_created_private(tmp_path, monkeypatch):
    blocker = tmp_path / "a_file"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", str(blocker / "sub"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    got = Path(claude_cli.ensure_claude_cwd())
    assert got == claude_cli._temp_cwd_candidate()
    assert got.stat().st_mode & 0o777 == 0o700


def test_neutral_cwd_falls_back_to_temp_when_uncreatable(tmp_path, monkeypatch):
    blocker = tmp_path / "a_file"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("LEETCOACH_CLAUDE_CWD", str(blocker / "sub"))
    got = Path(claude_cli.ensure_claude_cwd())
    assert got.is_dir()
    assert got != blocker / "sub"


def test_real_runner_runs_child_in_given_cwd(tmp_path):
    cwd = tmp_path / "neutral"
    cwd.mkdir()
    script = "import os, sys\nsys.stdin.read()\nprint(os.getcwd())\n"
    lines = list(claude_cli._real_runner([sys.executable, "-c", script], "", cwd=str(cwd)))
    assert Path(lines[0].strip()).resolve() == cwd.resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe shim quoting is Windows-only")
def test_empty_tools_arg_survives_a_cmd_shim(tmp_path):
    """`claude` is usually the npm `claude.cmd` shim (`"...claude.exe" %*`).
    The empty `--tools ""` element must reach the real binary as an empty
    argument, not vanish in cmd.exe's re-parse."""
    echo = tmp_path / "argv.py"
    echo.write_text(
        "import json, sys\nsys.stdin.read()\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    shim = tmp_path / "fake-claude.cmd"
    shim.write_text(
        f'@ECHO off\r\nSETLOCAL\r\n"{sys.executable}" "%~dp0argv.py"   %*\r\n',
        encoding="ascii",
    )
    tail = ["-p", "--tools", "", "--strict-mcp-config", "--system-prompt",
            prompts.TUTOR_SYSTEM_PROMPT, "--model", "m"]
    lines = list(claude_cli._real_runner([str(shim), *tail], "ping"))
    assert json.loads(lines[-1]) == tail


# --- personas ---------------------------------------------------------------

@pytest.mark.parametrize("persona", [
    prompts.TUTOR_SYSTEM_PROMPT,
    prompts.QUICK_ASK_SYSTEM_PROMPT,
    prompts.FOLLOWUP_SYSTEM_PROMPT,
    claude_cli.DEFAULT_SYSTEM_PROMPT,
    classifier.CLASSIFIER_SYSTEM_PROMPT,  # 3A C16: every persona, the classifier's too
])
def test_personas_are_single_line_and_cmd_safe(persona):
    # They travel in argv through the claude.cmd shim: a newline would cut the
    # command line and cmd metacharacters/quotes could be re-interpreted.
    assert persona and persona.isascii()
    for bad in ('"', "%", "!", "^", "&", "|", "<", ">", "\n", "\r"):
        assert bad not in persona, f"{bad!r} in persona"
    assert not persona.startswith("-")


def test_tutor_persona_is_mode_agnostic_and_names_leetcoach():
    p = prompts.TUTOR_SYSTEM_PROMPT
    assert "LeetCoach" in p and "Markdown" in p
    for mode_word in ("Guided", "Answer mode", "Learning mode"):
        assert mode_word not in p


# --- session_id capture -----------------------------------------------------

def _delta(text):
    return json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})


def test_session_id_captured_from_system_init():
    runner, _ = recording_runner([
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-123"}),
        _delta("A"),
        json.dumps({"type": "result", "subtype": "success", "result": "A",
                    "session_id": "sess-123"}),
    ])
    r = claude_cli.run("hi", runner=runner, flags=ALL_FLAGS)
    assert "".join(r) == "A"
    assert r.session_id == "sess-123"


def test_session_id_captured_from_result_when_no_init():
    runner, _ = recording_runner([
        _delta("A"),
        json.dumps({"type": "result", "subtype": "success", "result": "A",
                    "session_id": "sess-xyz"}),
    ])
    r = claude_cli.run("hi", runner=runner, flags=ALL_FLAGS)
    list(r)
    assert r.session_id == "sess-xyz"


def test_session_id_ignores_non_string_values():
    runner, _ = recording_runner([
        json.dumps({"type": "system", "subtype": "init", "session_id": 42}),
        _delta("A"),
        json.dumps({"type": "result", "subtype": "success", "result": "A",
                    "session_id": 7}),
    ])
    r = claude_cli.run("hi", runner=runner, flags=ALL_FLAGS)
    list(r)
    assert r.session_id is None


# --- app wiring: which calls persist, which carry which persona -------------

def test_app_passes_personas_and_persistence_per_call_kind(tmp_path, monkeypatch):
    from _helpers import CLASSIFY_JSON

    import app as app_module

    calls = []

    def fake_run(prompt, **kwargs):
        is_classify = "Classify the following" in prompt
        is_ask = "quick-reference assistant" in prompt
        calls.append({"kind": "classify" if is_classify else "ask" if is_ask else "study",
                      **kwargs})
        yield json.dumps(CLASSIFY_JSON) if is_classify else "Answer text.\n"

    application = app_module.create_app(run_fn=fake_run)
    application.config.update(TESTING=True)
    c = application.test_client()
    resp = c.post("/run", json={"problem": "P", "mode": "learning", "language": "python"})
    resp.get_data()
    resp = c.post("/ask", json={"question": "what is heapq?"})
    assert resp.status_code == 200

    by_kind = {k: [c_ for c_ in calls if c_["kind"] == k] for k in ("study", "classify", "ask")}
    study, = by_kind["study"]
    classify, = by_kind["classify"]
    ask, = by_kind["ask"]
    assert study["system_prompt"] == prompts.TUTOR_SYSTEM_PROMPT
    assert study["persist_session"] is True
    assert classify["persist_session"] is False
    assert classify["system_prompt"]
    assert ask["persist_session"] is False
    assert ask["system_prompt"] == prompts.QUICK_ASK_SYSTEM_PROMPT


def test_study_prompts_no_longer_carry_the_persona_line():
    for p in (
        prompts.build_learning("P", language="python"),
        prompts.build_answer("P", tier="normal", language="python"),
        prompts.build_guided("P", tier="normal", language="python"),
    ):
        assert "You are a patient coding tutor" not in p
        assert "You are an expert competitive-programming assistant" not in p
