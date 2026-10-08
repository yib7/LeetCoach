"""The dev harness's fake `claude` CLI (scripts/dev/fake_claude.py) speaks the
real CLI's dialect: its --help passes the flag probe, its auth status reads as
signed in, and its stream-json goes through the REAL parser / classifier /
sandbox exactly like a real run. Nothing here calls real Claude - the "CLI" is
a local Python script printing canned text."""
from __future__ import annotations

import importlib.util
import json
import os
import re
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


def test_run_fake_does_not_reseed_under_a_running_instance(monkeypatch):
    # SP5 fix R6: a second launch must not wipe the live scratch library.
    run_fake = _load_run_fake()

    def boom(*args, **kwargs):
        raise AssertionError("must not configure/seed while an instance is running")

    monkeypatch.setattr(run_fake, "configure", boom)
    monkeypatch.setattr(run_fake, "seed", boom)
    assert run_fake.main([], probe=lambda port: f"http://127.0.0.1:{port}/") == 0


def test_running_instance_accepts_only_leetcoach(monkeypatch):
    run_fake = _load_run_fake()

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return self.body

    ok = run_fake.running_instance(5057, opener=lambda u, timeout: Resp(b'{"app": "leetcoach"}'))
    assert ok == "http://127.0.0.1:5057/"
    other = run_fake.running_instance(5057, opener=lambda u, timeout: Resp(b'{"app": "x"}'))
    assert other is None

    def refused(u, timeout):
        raise OSError("refused")

    assert run_fake.running_instance(5057, opener=refused) is None


# ---- SP6: the fake's docs follow the D2 contract ------------------------------

def _doc(flags, mode, problem=TWO_SUM, *, language="python", crlf=False):
    if mode == "learning":
        prompt = prompts.build_learning(problem, language=language)
    elif mode == "guided":
        prompt = prompts.build_guided(problem, tier="normal", language=language)
    else:
        prompt = prompts.build_answer(problem, tier="normal", language=language)
    if crlf:
        prompt = prompt.replace("\n", "\r\n")
    return "".join(claude_cli.run(prompt, runner=_runner(), flags=flags, model="opus"))


def _h2(text):
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


@pytest.mark.parametrize("mode", ["answer", "guided", "learning"])
def test_fake_doc_has_the_contract_header_and_sections(flags, mode):
    text = _doc(flags, mode)
    lines = text.splitlines()
    assert lines[0] == "# Two Sum"
    assert lines[1].startswith("Pattern: Arrays & Hashing · Difficulty: ")
    assert _h2(text) == list(prompts.doc_sections(mode))
    prose = re.sub(r"```.*?```", "", text.split("## Flashcards")[0], flags=re.S)
    assert "?" not in prose.replace("?a=1", "")  # no questions to the reader


def test_fake_guided_doc_has_hints_brute_force_and_one_solution_block(flags):
    text = _doc(flags, "guided")
    for n in range(1, 5):
        assert f"### Hint {n}" in text
    assert text.index("### Hint 4") < text.index("## Solution")
    assert "brute force" in text.lower()
    assert text.count("```python solution") == 1
    code = parsing.extract_code(text, "python")
    assert "json.dumps(" in code
    assert sandbox.verify_answer(code, TWO_SUM, "python").status == "pass"


def test_fake_learning_doc_has_hints_and_no_solution(flags):
    text = _doc(flags, "learning")
    assert "### Hint 1" in text and "### Hint 4" in text
    assert "## Solution" not in text and "solution" not in "".join(
        line for line in text.splitlines() if line.startswith("```"))


def test_fake_answer_doc_has_no_hint_ladder(flags):
    text = _doc(flags, "answer")
    assert "### Hint" not in text
    assert text.count("```python solution") == 1


def test_fake_reads_number_and_difficulty_from_the_paste(flags):
    text = _doc(flags, "guided", "1. Two Sum\nMedium\n\n" + TWO_SUM.split("\n", 1)[1])
    assert text.startswith("# 1. Two Sum\nPattern: Arrays & Hashing · Difficulty: Medium\n")


def test_fake_contract_survives_crlf_prompts(flags):
    text = _doc(flags, "guided", crlf=True)
    assert "### Hint 1" in text and _h2(text) == list(prompts.doc_sections("guided"))


def test_seed_has_a_run_log_problem_records_and_legacy_files(tmp_path):
    import problem_store
    import stats

    run_fake = _load_run_fake()
    out = tmp_path / "output"
    run_fake.seed(out)
    entries = problem_store.read_runs(root=out)
    assert len(entries) >= 3
    logged = {f for e in entries for f in e["files"]}
    for rel in logged:
        assert (out / rel).is_file(), rel
    for e in entries:
        rec = problem_store.load_problem(e["problem_id"], root=out)
        assert rec and rec["difficulty"] in ("Easy", "Medium", "Hard")
    files = [{"path": p.relative_to(out).as_posix(), "mtime": p.stat().st_mtime}
             for p in out.rglob("*") if p.is_file() and ".leetcoach" not in p.parts]
    legacy = [f for f in files if f["path"] not in logged and f["path"].count("/") >= 2]
    assert legacy, "some seeded runs must have no log entry (legacy fallback)"
    s = stats.compute_stats(entries, files)
    assert s["sources"]["log"] >= 3 and s["sources"]["legacy"] >= 3
    assert s["currentStreak"] >= 4
    guided = [p for p in logged if p.startswith("guided/")]
    assert guided
    text = (out / guided[0]).read_text(encoding="utf-8")
    assert "### Hint 1" in text and "## Solution" in text


# ---- SP6 fix round: the fixed H3 after the hint ladder (I1), no solution in Learning (O4)

def _h3_after_hint4(text):
    tail = text.split("### Hint 4", 1)[1].split("\n## ", 1)[0]
    return [line[4:].strip() for line in tail.splitlines() if line.startswith("### ")]


@pytest.mark.parametrize("mode, heading", [("guided", "Walkthrough"), ("learning", "Techniques")])
def test_fake_doc_has_the_fixed_heading_after_the_hints(flags, mode, heading):
    assert _h3_after_hint4(_doc(flags, mode)) == [heading]


def test_fake_nowalk_marker_drops_the_heading_after_the_hints(flags):
    text = _doc(flags, "guided", TWO_SUM + "\nFAKE_NOWALK")
    assert _h3_after_hint4(text) == []
    approach = text.split("### Hint 4", 1)[1].split("\n## ", 1)[0]
    assert "Brute force" in approach and "Optimal" in approach


@pytest.mark.parametrize("language", ["python", "cpp", "java"])
def test_fake_learning_doc_has_no_solution_shaped_code(flags, language):
    text = _doc(flags, "learning", language=language)
    assert "return [" not in text and "for i, x" not in text
    assert "seen[target - x]" not in text


def test_seed_has_a_no_walkthrough_guided_doc_and_no_guided_tier(tmp_path):
    import problem_store

    run_fake = _load_run_fake()
    out = tmp_path / "output"
    run_fake.seed(out)
    text = (out / "guided" / "stack" / "20_valid_parentheses.md").read_text(encoding="utf-8")
    assert "### Hint 4" in text and _h3_after_hint4(text) == []
    for e in problem_store.read_runs(root=out):
        if e["mode"] != "answer":
            assert e["tier"] is None
    learning = (out / "learning" / "hash_map_learning" / "1_two_sum.md").read_text("utf-8")
    assert "return [" not in learning and "### Techniques" in learning
