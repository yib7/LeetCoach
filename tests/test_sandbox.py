"""Tests for `sandbox.py` — sample-I/O verification of generated solutions.

Unlike the rest of the suite (which mocks Claude), these tests run REAL local
Python subprocesses via ``sys.executable``. That's fine: it's local, free, and
involves zero Claude — the sandbox's whole job is to run untrusted *Python* code,
so exercising it for real is the only honest test.

Covered:
  * a known-GOOD snippet (reads stdin, prints the right answer) -> ``pass``;
  * a known-BAD snippet (prints the wrong answer) -> ``fail``;
  * a syntax-error snippet -> ``error``;
  * ``parse_samples`` pulls the Input/Output pair from a Two-Sum-style problem;
  * cpp/java with no compiler on PATH -> ``not_verified`` with a clear note;
  * the secret-free env: the child can't see a planted secret variable.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

import sandbox

# --- verify_python: pass / fail / error ----------------------------------

GOOD_DOUBLE = (
    "import sys\n"
    "n = int(sys.stdin.readline())\n"
    "print(n * 2)\n"
)

BAD_DOUBLE = (
    "import sys\n"
    "n = int(sys.stdin.readline())\n"
    "print(n * 3)\n"   # wrong: triples instead of doubles
)

SYNTAX_ERROR = "def f(:\n    pass\n"   # invalid syntax -> nonzero exit


def test_known_good_snippet_passes():
    r = sandbox.verify_python(GOOD_DOUBLE, "21\n", "42")
    assert r.status == "pass", r
    assert r.passed is True
    assert r.samples_passed == 1
    assert r.samples_total == 1


def test_known_bad_snippet_fails():
    r = sandbox.verify_python(BAD_DOUBLE, "21\n", "42")
    assert r.status == "fail", r
    assert r.passed is False
    assert r.samples_passed == 0


def test_syntax_error_snippet_errors():
    r = sandbox.verify_python(SYNTAX_ERROR, "21\n", "42")
    assert r.status == "error", r
    assert r.passed is False


def test_trailing_whitespace_is_normalized():
    # expected has no trailing newline; the program prints one -> still a pass.
    r = sandbox.verify_python("print('hello')\n", "", "hello")
    assert r.status == "pass", r


def test_timeout_is_an_error():
    # An infinite loop must hit the timeout and be reported as `error`, not hang.
    loop = "while True:\n    pass\n"
    r = sandbox.verify_python(loop, "", "anything", timeout=2)
    assert r.status == "error", r
    assert "out" in r.note.lower()  # "timed out"


# --- timeout tree-kill + bounded output (audit6 P1-2 step 1) --------------

def _windows_pid_alive(pid: int) -> bool:
    """True if ``pid`` is a live process on Windows.

    Probe choice: ``OpenProcess`` + ``GetExitCodeProcess`` == ``STILL_ACTIVE``
    (259) via ctypes. ``os.kill(pid, 0)`` is NOT usable on Windows — it calls
    ``TerminateProcess`` (it would kill the grandchild and make the test pass
    vacuously) — and parsing ``tasklist`` output is locale-dependent. A process
    that has exited reports its real exit code (or ``OpenProcess`` fails once
    all handles are gone), so ``STILL_ACTIVE`` is a dependable liveness signal
    for a grandchild that sleeps 60s and never exits code 259 on its own.
    """
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only grandchild-kill probe")
def test_timeout_kills_grandchildren_on_windows(tmp_path):
    """On timeout the WHOLE process tree must die, not just the direct child.

    The untrusted solution spawns a grandchild (60s sleeper), reports its PID
    through a file (path passed via stdin), then sleeps past the timeout. After
    verify_python returns, the grandchild must be gone — the old plain
    ``subprocess.run(timeout=...)`` only killed the direct child.

    The wall-time bound below is load-bearing: the old implementation ALSO
    blocked ~58s in its post-kill ``communicate()`` (the grandchild holds the
    inherited stdout pipe open), by which point the grandchild had exited
    naturally — making a liveness probe alone pass vacuously."""
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        "pidfile = sys.stdin.readline().strip()\n"
        "gc = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "with open(pidfile, 'w') as f:\n"
        "    f.write(str(gc.pid))\n"
        "time.sleep(60)\n"
    )
    start = time.monotonic()
    r = sandbox.verify_python(code, str(pidfile) + "\n", "whatever", timeout=2)
    elapsed = time.monotonic() - start
    assert elapsed < 10, (
        f"took {elapsed:.1f}s -- verify_python blocked on the grandchild's pipe"
    )
    assert r.status == "error", r
    assert "timed out" in r.note

    assert pidfile.exists(), "child never reported a grandchild PID (test setup)"
    gc_pid = int(pidfile.read_text().strip())
    try:
        # taskkill is near-instant but asynchronous at the margins: poll briefly.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _windows_pid_alive(gc_pid):
            time.sleep(0.1)
        assert not _windows_pid_alive(gc_pid), (
            f"grandchild {gc_pid} survived the timeout tree-kill"
        )
    finally:
        # Never leak a 60s sleeper into the host, even when the assert fails.
        subprocess.run(
            ["taskkill", "/F", "/PID", str(gc_pid)],
            capture_output=True, check=False,
        )


def test_runaway_output_is_bounded_and_killed_promptly():
    """A tight print loop must not buffer unbounded output in memory, and the
    child must be killed as soon as the cap is exceeded — well before the
    timeout. The stored stdout stays within _OUTPUT_LIMIT plus a short
    truncation marker."""
    spam = (
        "import sys, time\n"
        "chunk = 'x' * 65536\n"
        "for _ in range(256):\n"      # ~16 MB if left unbounded
        "    sys.stdout.write(chunk)\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"            # never exits on its own
    )
    start = time.monotonic()
    r = sandbox.verify_python(spam, "", "whatever", timeout=30)
    elapsed = time.monotonic() - start
    # Generous wall bound: the overflow kill fires within ~1s in practice; the
    # old behavior sat in communicate() for the full 30s timeout.
    assert elapsed < 15, f"took {elapsed:.1f}s -- output cap did not kill the child"
    assert r.status == "error", r
    assert "exceed" in r.note.lower(), r.note
    assert r.detail, "overflow verdict should carry the captured (bounded) output"
    stored = r.detail[0].get("stdout", "")
    assert len(stored) <= sandbox._OUTPUT_LIMIT + 64, (
        f"stored stdout not bounded: {len(stored)} chars"
    )


# --- Windows Job Object caps (audit6 P1-2 step 2) -------------------------

@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object caps")
def test_memory_hog_is_killed_by_job_cap_on_windows():
    """Allocating far past the 512 MB job cap must fail INSIDE the child
    (MemoryError -> nonzero exit -> ``error``) — fast, not via timeout.

    Built-in cap detection: if the job caps silently failed to apply, the 1 GB
    allocation succeeds, the child prints and exits 0, and the run is a
    ``fail`` (wrong output) — flunking the status assert. If instead the child
    somehow hung, the elapsed/note asserts reject the timeout path."""
    hog = (
        "data = bytearray(1024 * 1024 * 1024)\n"   # 1 GB >> 512 MB cap
        "print('ALLOCATED', len(data))\n"
    )
    start = time.monotonic()
    r = sandbox.verify_python(hog, "", "whatever", timeout=10)
    elapsed = time.monotonic() - start
    assert elapsed < 8, f"took {elapsed:.1f}s -- memory cap did not fire fast"
    assert r.status == "error", r
    assert "exited with code" in r.note, r.note  # MemoryError, not a timeout


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object caps")
def test_fork_bomb_is_stopped_by_active_process_cap_on_windows():
    """Spawning more processes than the job's active-process cap (16) must
    fail inside the child: CreateProcess is denied (OSError) once the job is
    full, and the child exits with the marker code 7 -> ``error``.

    Built-in cap detection: with no cap all 32 spawns succeed, the child
    prints the marker and exits 0 -> ``pass`` (matched output) — flunking the
    status assert. The ~15 sleepers that DID spawn before the wall are inside
    the job, so close_job's KILL_ON_JOB_CLOSE in verify_python's finally reaps
    them."""
    bomb = (
        "import subprocess, sys\n"
        "procs = []\n"
        "try:\n"
        "    for _ in range(32):\n"
        "        procs.append(subprocess.Popen(\n"
        "            [sys.executable, '-c', 'import time; time.sleep(20)']))\n"
        "except OSError:\n"
        "    sys.exit(7)\n"   # the cap said no -- the expected path
        "print('SPAWNED-ALL')\n"
    )
    start = time.monotonic()
    r = sandbox.verify_python(bomb, "", "SPAWNED-ALL", timeout=30)
    elapsed = time.monotonic() - start
    assert elapsed < 25, f"took {elapsed:.1f}s -- process cap did not stop the bomb"
    assert r.status == "error", r
    assert "exited with code 7" in r.note, r.note


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object caps")
def test_memory_cap_applies_on_first_call_in_fresh_interpreter():
    """Regression for the COLD-START race: the FIRST verify_python call of a
    fresh process must already be capped.

    The original implementation did its ctypes imports lazily inside the
    helper, so the first call in a process paid ~100ms of import cost AFTER
    Popen had spawned the child — the child finished interpreter startup and
    committed its 1 GB before the job was ever assigned. A warm pytest
    process never sees that (earlier tests pre-import ctypes), which is
    exactly why this probe runs in a brand-new python subprocess: its first
    verification IS the cold path, same as the Flask app's first run."""
    probe = (
        "import sandbox\n"
        "hog = 'data = bytearray(1024 * 1024 * 1024)\\nprint(\"survived\")\\n'\n"
        "r = sandbox.verify_python(hog, '', 'whatever', timeout=10)\n"
        "print('PROBE', r.status, r.note)\n"
    )
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=45,
        cwd=repo_root,   # `-c` puts the cwd on sys.path -> `import sandbox` works
    )
    assert out.returncode == 0, (out.stdout, out.stderr)
    marker = [ln for ln in out.stdout.splitlines() if ln.startswith("PROBE ")]
    assert marker, (out.stdout, out.stderr)
    assert marker[-1].startswith("PROBE error"), (
        f"first-call cap escaped in a fresh interpreter: {marker[-1]!r}"
    )


def test_verify_works_when_job_caps_unavailable(monkeypatch):
    """Graceful degradation: if the job APIs fail (old Windows, unexpected
    environment) verification must proceed WITHOUT the caps, never break.
    Simulated by forcing the pre-spawn job creation to report failure (None)."""
    monkeypatch.setattr(sandbox, "create_job_with_caps", lambda *a, **k: None)
    r = sandbox.verify_python(GOOD_DOUBLE, "21\n", "42")
    assert r.status == "pass", r


# --- secret-free environment ---------------------------------------------

def test_child_cannot_read_a_planted_secret(monkeypatch):
    monkeypatch.setenv("LEETCOACH_FAKE_SECRET", "topsecret")
    code = (
        "import os\n"
        "print('SECRET' if os.environ.get('LEETCOACH_FAKE_SECRET') else 'NONE')\n"
    )
    r = sandbox.verify_python(code, "", "NONE")
    assert r.status == "pass", r  # the child saw NONE -> secret did not leak


# --- parse_samples -------------------------------------------------------

TWO_SUM_PROBLEM = """\
Two Sum

Given an array of integers nums and an integer target, return indices of the two
numbers such that they add up to target.

Example 1:

Input: nums = [2,7,11,15], target = 9
Output: [0,1]
Explanation: Because nums[0] + nums[1] == 9, we return [0, 1].

Example 2:

Input: nums = [3,2,4], target = 6
Output: [1,2]

Constraints:
  2 <= nums.length <= 10^4
"""


def test_parse_samples_extracts_two_sum_pairs():
    samples = sandbox.parse_samples(TWO_SUM_PROBLEM)
    assert len(samples) == 2
    first = samples[0]
    assert "nums = [2,7,11,15], target = 9" in first.stdin
    assert first.expected_stdout == "[0,1]"
    second = samples[1]
    assert "nums = [3,2,4], target = 6" in second.stdin
    assert second.expected_stdout == "[1,2]"


def test_parse_samples_returns_empty_when_none():
    assert sandbox.parse_samples("just some prose, no examples here") == []
    assert sandbox.parse_samples("") == []


# A multi-line Input block pushes Output past the old fixed 4-line window.
MULTILINE_INPUT_PROBLEM = """\
Example 1:

Input:
grid = [
  [1, 0, 0],
  [0, 1, 0],
  [0, 0, 1]
]
target = 3
Output: [2,2]
Explanation: the diagonal sums to target.
"""


def test_parse_samples_finds_output_past_multiline_input():
    """A multi-line Input block must not push the Output out of range. Regression
    guard for audit P2 #6 (fixed 4-line window silently dropped the pair, so
    verification degraded to 'not auto-verified').

    Also guards audit P1-1: the body below a bare ``Input:`` label IS the stdin —
    it must not be dropped (which yielded ``stdin == '\\n'`` and a false FAIL)."""
    samples = sandbox.parse_samples(MULTILINE_INPUT_PROBLEM)
    assert len(samples) == 1, f"expected the pair to be found, got {samples}"
    assert samples[0].expected_stdout == "[2,2]"
    stdin = samples[0].stdin
    assert "grid" in stdin, f"multi-line Input body was dropped: {stdin!r}"
    assert "[1, 0, 0]," in stdin
    assert "[0, 0, 1]" in stdin
    assert "target = 3" in stdin
    assert stdin.endswith("\n")  # synthesized stdin keeps its trailing newline


# `Output:` on its own line with the value below (common for 2-D results).
MULTILINE_OUTPUT_PROBLEM = """\
Example 1:

Input: root = [3,9,20,null,null,15,7]
Output:
[[3],[9,20],[15,7]]

Explanation: level order traversal.
"""


def test_parse_samples_captures_output_on_following_line():
    """A bare ``Output:`` label with its value on the next line(s) must capture
    that value, not an empty expected_stdout (audit P1-1)."""
    samples = sandbox.parse_samples(MULTILINE_OUTPUT_PROBLEM)
    assert len(samples) == 1, f"expected the pair to be found, got {samples}"
    assert "root = [3,9,20,null,null,15,7]" in samples[0].stdin
    assert samples[0].expected_stdout == "[[3],[9,20],[15,7]]"


def test_parse_samples_drops_pair_when_both_sides_empty():
    """Bare ``Input:`` / ``Output:`` labels with no data anywhere must yield NO
    sample (caller falls back to 'not auto-verified') instead of a bogus
    ``('\\n', '')`` pair that false-FAILs a correct solution (audit P1-1)."""
    text = (
        "Example 1:\n"
        "Input:\n"
        "Output:\n"
        "\n"
        "Constraints:\n"
        "  1 <= n <= 10\n"
    )
    assert sandbox.parse_samples(text) == []


def test_parse_samples_stops_at_section_marker_when_no_output():
    """An Input: with no Output: before the next section must NOT be paired with
    a later example's Output (the scan bails at the section marker)."""
    text = (
        "Example 1:\n"
        "Input: nums = [1,2]\n"
        "Explanation: no output line here at all.\n"
        "\n"
        "Example 2:\n"
        "Input: nums = [3,4]\n"
        "Output: [0,1]\n"
    )
    samples = sandbox.parse_samples(text)
    # Only the well-formed second pair should be captured.
    assert len(samples) == 1
    assert "nums = [3,4]" in samples[0].stdin
    assert samples[0].expected_stdout == "[0,1]"


# --- verify_answer orchestrator: python first-class ----------------------

ECHO_TARGET_SOLUTION = (
    # Reads the whole 'Input: ...' line off stdin and prints a fixed answer so we
    # can drive verify_answer end-to-end without parsing the LeetCode arg syntax.
    "import sys\n"
    "line = sys.stdin.readline()\n"
    "print('[0,1]')\n"
)

SINGLE_SAMPLE_PROBLEM = """\
Example 1:
Input: nums = [2,7,11,15], target = 9
Output: [0,1]
"""


def test_verify_answer_python_pass():
    r = sandbox.verify_answer(ECHO_TARGET_SOLUTION, SINGLE_SAMPLE_PROBLEM, "python")
    assert r.status == "pass", r
    assert r.samples_total == 1


def test_verify_answer_python_fail():
    wrong = "print('[9,9]')\n"
    r = sandbox.verify_answer(wrong, SINGLE_SAMPLE_PROBLEM, "python")
    assert r.status == "fail", r


def test_verify_answer_no_samples_is_not_verified():
    r = sandbox.verify_answer("print('hi')\n", "prose with no examples", "python")
    assert r.status == "not_verified", r
    assert "no sample" in r.note.lower()


def test_verify_answer_never_raises_on_garbage():
    # None code / None problem must degrade, not explode.
    r = sandbox.verify_answer(None, None, "python")
    assert r.status == "not_verified", r


# --- verify_answer: cpp/java with no compiler ----------------------------

@pytest.mark.parametrize("lang,compiler", [("cpp", "g++"), ("java", "javac")])
def test_cpp_java_without_compiler_is_not_verified(lang, compiler, monkeypatch):
    # Force shutil.which to report the compiler missing, regardless of the host.
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    r = sandbox.verify_answer("// some code", SINGLE_SAMPLE_PROBLEM, lang)
    assert r.status == "not_verified", r
    assert compiler in r.note
    assert "path" in r.note.lower()


def test_cpp_with_compiler_present_is_not_verified_but_notes_it(monkeypatch):
    # Even if a compiler IS on PATH, cpp/java auto-run is out of MVP scope.
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/" + name)
    r = sandbox.verify_answer("int main(){}", SINGLE_SAMPLE_PROBLEM, "cpp")
    assert r.status == "not_verified", r
    assert "not supported" in r.note.lower() or "manually" in r.note.lower()


def test_unsupported_language_is_not_verified():
    r = sandbox.verify_answer("fn main(){}", SINGLE_SAMPLE_PROBLEM, "rust")
    assert r.status == "not_verified", r


# --- temp dir cleanup ----------------------------------------------------

def test_run_dir_is_cleaned_up(tmp_path, monkeypatch):
    # Point tempfile at a known dir and confirm no leftover leetcoach_run_* dirs.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    import tempfile as _tf
    monkeypatch.setattr(_tf, "tempdir", None)  # force re-read of env
    sandbox.verify_python("print('x')\n", "", "x")
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("leetcoach_run_")]
    assert leftovers == [], f"temp run dirs were not cleaned: {leftovers}"
