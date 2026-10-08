"""The dev harness's fake `claude` CLI (scripts/dev/fake_claude.py) speaks the
real CLI's dialect: its --help passes the flag probe, its auth status reads as
signed in, and its stream-json goes through the REAL parser / classifier /
sandbox exactly like a real run. Nothing here calls real Claude - the "CLI" is
a local Python script printing canned text."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app
import classifier
import claude_cli
import parsing
import prompts
import sandbox

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / "scripts" / "dev"
FAKE = DEV / "fake_claude.py"

TWO_SUM = (
    "Two Sum\n\nGiven an array of integers nums and an integer target, return "
    "indices of the two numbers such that they add up to target.\n\n"
    "Example:\nInput: nums = [2,7,11,15], target = 9\nOutput: [0,1]\n\n"
    "Constraints:\n2 <= nums.length <= 10^4"
)


def _fake(*args: str, stdin: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.pop("FAKE_CLAUDE_FAIL", None)
    full_env["FAKE_CLAUDE_DELAY"] = "0"
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, str(FAKE), *args], input=stdin.encode("utf-8"),
        capture_output=True, env=full_env, timeout=60,
    )


def _runner(env: dict | None = None):
    """A claude_cli runner that executes the fake with the app's real argv."""
    def run(argv, stdin_text, *, cwd=None, handle=None):
        proc = _fake(*argv[1:], stdin=stdin_text, env=env)
        if proc.returncode != 0 and not proc.stdout:
            raise claude_cli.ClaudeUnavailableError(proc.stderr.decode("utf-8", "replace"))
        return proc.stdout.decode("utf-8").splitlines()
    return run


@pytest.fixture
def flags():
    return claude_cli.parse_help_flags(_fake("--help").stdout.decode("utf-8"))


def test_help_lists_every_flag_the_app_probes_for(flags):
    for flag in (
        claude_cli.FLAG_SAFE_MODE, claude_cli.FLAG_TOOLS, claude_cli.FLAG_STRICT_MCP,
        claude_cli.FLAG_SYSTEM_PROMPT, claude_cli.FLAG_NO_PERSIST,
    ):
        assert flag in flags
    assert not flags & claude_cli.FORBIDDEN_FLAGS


def test_auth_status_reports_signed_in():
    proc = _fake("auth", "status")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["loggedIn"] is True
    signed_out = _fake("auth", "status", env={"FAKE_CLAUDE_FAIL": "auth"})
    assert json.loads(signed_out.stdout)["loggedIn"] is False


def _study(flags, problem=TWO_SUM, *, language="python", env=None, model="opus"):
    prompt = prompts.build_answer(problem, tier="normal", language=language)
    handle = claude_cli.run(
        prompt, runner=_runner(env), flags=flags, model=model,
        system_prompt=prompts.TUTOR_SYSTEM_PROMPT, persist_session=True,
    )
    return handle, "".join(handle)


def test_study_stream_parses_with_model_session_and_a_verifiable_solution(flags):
    handle, text = _study(flags)
    assert handle.model == "claude-opus-5-5"
    assert handle.session_id
    assert text.startswith("# Two Sum")
    assert "**Input:**" in text and "**Output:**" in text
    code = parsing.extract_code(text, "python")
    assert "sys.stdin.read()" in code
    result = sandbox.verify_answer(code, TWO_SUM, "python")
    assert result.status == "pass", result.note


@pytest.mark.parametrize("marker, status", [("FAKE_WRONG", "fail"), ("FAKE_CRASH", "error")])
def test_in_band_markers_change_the_verdict(flags, marker, status):
    problem = TWO_SUM + "\n" + marker
    _, text = _study(flags, problem)
    code = parsing.extract_code(text, "python")
    assert sandbox.verify_answer(code, problem, "python").status == status


def test_model_alias_maps_to_a_concrete_id(flags):
    handle, _ = _study(flags, model="sonnet")
    assert handle.model == "claude-sonnet-5-5"


def test_other_languages_get_their_own_solution_block(flags):
    _, text = _study(flags, language="java")
    assert "```java solution" in text
    assert "class Solution" in parsing.extract_code(text, "java")


def test_learning_doc_has_an_untagged_block_and_no_solution(flags):
    prompt = prompts.build_learning(TWO_SUM, language="python")
    text = "".join(claude_cli.run(prompt, runner=_runner(), flags=flags, model="opus"))
    assert "\n```\n" in text
    assert "```python solution" not in text


def test_fake_fail_ends_in_an_error_result(flags):
    with pytest.raises(claude_cli.ClaudeUnavailableError, match="Simulated failure"):
        _study(flags, TWO_SUM + "\nFAKE_FAIL")


def test_classifier_reads_the_fake_json(flags):
    def run_fn(prompt, **kw):
        return claude_cli.run(prompt, runner=_runner(), flags=flags, model="haiku", **kw)

    result = classifier.classify(TWO_SUM, run_fn=run_fn)
    assert result.problem_type == "hash_map"
    assert result.topics


def test_quick_ask_answer(flags):
    prompt = prompts.build_quick_ask("C++ syntax for a min-heap?", language="cpp")
    handle = claude_cli.run(
        prompt, runner=_runner(), flags=flags, model="haiku",
        system_prompt=prompts.QUICK_ASK_SYSTEM_PROMPT,
    )
    text = "".join(handle)
    assert "heapq" in text
    assert "C++ syntax for a min-heap?" in text
    assert handle.model == "claude-haiku-5-5"


def test_startup_failure_knob():
    proc = _fake("-p", env={"FAKE_CLAUDE_FAIL": "start"})
    assert proc.returncode == 1
    assert b"simulated startup failure" in proc.stderr


@pytest.mark.skipif(os.name != "nt", reason="the .cmd shim is Windows-only")
def test_cmd_shim_through_the_real_runner(monkeypatch, flags):
    """End to end on the real subprocess runner: cmd.exe shim -> python fake."""
    monkeypatch.setenv("LEETCOACH_CLAUDE_BIN", str(DEV / "fake_claude.cmd"))
    monkeypatch.setenv("FAKE_CLAUDE_PYTHON", sys.executable)
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0")
    monkeypatch.delenv("FAKE_CLAUDE_FAIL", raising=False)
    prompt = prompts.build_answer(TWO_SUM, tier="normal", language="python")
    handle = claude_cli.run(
        prompt, flags=flags, model="opus", system_prompt=prompts.TUTOR_SYSTEM_PROMPT,
    )
    text = "".join(handle)
    assert handle.model == "claude-opus-5-5"
    assert "```python solution" in text


# ---- run_fake.py: the seeded scratch library --------------------------------

def _load_run_fake():
    spec = importlib.util.spec_from_file_location("run_fake", DEV / "run_fake.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_seed_builds_a_multi_day_library_with_every_verdict(tmp_path):
    run_fake = _load_run_fake()
    out = tmp_path / "output"
    run_fake.seed(out)
    verdicts = set()
    mtimes = set()
    for md in out.rglob("*.md"):
        verdicts.add(app.verdict_from_text(md.read_text(encoding="utf-8")))
        mtimes.add(int(md.stat().st_mtime // 86400))
    assert {"pass", "fail", "error", "not_verified", None} <= verdicts
    assert len(mtimes) >= 4
    run_fake.seed(out)  # re-seeding its own scratch dir is allowed


def test_seed_refuses_to_wipe_a_directory_it_did_not_create(tmp_path):
    run_fake = _load_run_fake()
    precious = tmp_path / "output"
    precious.mkdir()
    (precious / "notes.md").write_text("mine", encoding="utf-8")
    with pytest.raises(SystemExit):
        run_fake.seed(precious)
    assert (precious / "notes.md").exists()


def test_configure_points_everything_at_scratch(tmp_path, monkeypatch):
    run_fake = _load_run_fake()
    # Register every key configure() writes, so monkeypatch restores them all.
    for key in (
        "LEETCOACH_CLAUDE_BIN", "LEETCOACH_OUTPUT_DIR", "LEETCOACH_TOPIC_INDEX",
        "LEETCOACH_CLAUDE_CWD", "LEETCOACH_NO_DOTENV", "LEETCOACH_DOTENV_PATH",
        "LEETCOACH_NO_BROWSER", "FAKE_CLAUDE_PYTHON",
    ):
        monkeypatch.setenv(key, "placeholder")
    out = run_fake.configure(tmp_path)
    assert os.environ["LEETCOACH_OUTPUT_DIR"] == str(out)
    assert os.environ["LEETCOACH_NO_DOTENV"] == "1"
    assert os.environ["LEETCOACH_NO_BROWSER"] == "1"
    assert Path(os.environ["LEETCOACH_DOTENV_PATH"]).parent == tmp_path
    assert Path(os.environ["LEETCOACH_CLAUDE_BIN"]).name.startswith("fake_claude")
