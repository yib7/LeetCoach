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

import parsing
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


def test_timeout_carries_partial_output_and_sample_context_in_detail():
    """#4 regression: a timeout used to report a bare "timed out after Xs"
    note with NO `detail` at all, so the saved .md couldn't show the stdin,
    the expected output, or whatever the wedged solution had already printed
    before it was killed. `detail` must carry all of that."""
    code = (
        "import sys\n"
        "print('partial output before hanging')\n"
        "sys.stdout.flush()\n"
        "while True:\n"
        "    pass\n"
    )
    r = sandbox.verify_python(code, "the-stdin\n", "the-expected", timeout=2)
    assert r.status == "error", r
    assert "timed out" in r.note
    assert r.detail, "timeout verdict must carry a detail entry"
    entry = r.detail[0]
    assert entry["stdin"] == "the-stdin\n"
    assert entry["expected"] == "the-expected"
    assert "partial output before hanging" in entry["stdout"]


# --- A3: structural (tolerant) output comparison --------------------------

def test_outputs_match_exact_is_always_a_match():
    assert sandbox._outputs_match("[0,1]", "[0,1]") is True


def test_outputs_match_two_sum_list_spacing_regression():
    # The real recorded bug: a correct Two Sum answer prints `[0, 1]` (a space
    # after the comma, Python's default list repr) against an expected
    # `[0,1]` (LeetCode's compact style) and used to false-FAIL.
    assert sandbox._outputs_match("[0, 1]", "[0,1]") is True


def test_outputs_match_bool_vs_json_lowercase():
    assert sandbox._outputs_match("True", "true") is True
    assert sandbox._outputs_match("False", "false") is True
    assert sandbox._outputs_match("[True, False]", "[true, false]") is True


def test_outputs_match_none_vs_json_null():
    assert sandbox._outputs_match("None", "null") is True
    assert sandbox._outputs_match("[1, None, 3]", "[1, null, 3]") is True


def test_outputs_match_float_tolerance():
    assert sandbox._outputs_match("3.14159", "3.141590001") is True
    assert sandbox._outputs_match("2.0", "1.0") is False


def test_outputs_match_int_vs_int_requires_exact_value():
    """#1 regression: `math.isclose`'s default `rel_tol=1e-5` is scaled by
    magnitude, so large-but-different plain ints were wrongly reported equal
    even though neither side is a float. The tolerance must apply ONLY when
    at least one side is a float; two ints must compare exactly."""
    assert sandbox._outputs_match("100001", "100000") is False
    assert sandbox._outputs_match("1000000008", "1000000007") is False
    assert sandbox._outputs_match("[100001]", "[100000]") is False
    # sanity: the exact-match case still passes.
    assert sandbox._outputs_match("100000", "100000") is True


def test_outputs_match_json_keyword_inside_quoted_string_is_not_rewritten():
    """#8 regression: a naive `\\bnull\\b` substitution rewrote the "null"
    inside the STRING `"not null"` into `"not None"`, making two genuinely
    different string values compare equal."""
    assert sandbox._outputs_match('[true, "not None"]', '[true, "not null"]') is False
    # the bare (unquoted) keyword outside any string is still translated.
    assert sandbox._outputs_match("[true, null]", "[true, null]") is True


def test_outputs_match_unquoted_vs_quoted_string():
    assert sandbox._outputs_match("hello", '"hello"') is True
    assert sandbox._outputs_match("hello", "'hello'") is True


def test_outputs_match_genuinely_wrong_value_stays_a_mismatch():
    assert sandbox._outputs_match("[9,9]", "[0,1]") is False
    assert sandbox._outputs_match("43", "42") is False


def test_outputs_match_order_matters_by_default():
    # Without "any order" in the problem text, a permutation is NOT a match —
    # never silently turn a wrong (order-dependent) answer into a pass.
    assert sandbox._outputs_match("[1,0]", "[0,1]", "") is False


def test_outputs_match_any_order_allowed_when_problem_says_so():
    problem = "Return the two indices, in any order."
    assert sandbox._outputs_match("[1,0]", "[0,1]", problem) is True


def test_outputs_match_any_order_still_rejects_different_multiset():
    problem = "You may return the answer in any order."
    assert sandbox._outputs_match("[0,2]", "[0,1]", problem) is False


def test_outputs_match_unparseable_falls_back_to_exact_normalized():
    # Neither side parses as a literal/JSON value at all after the bare-word
    # fallback still disagrees -> must not silently pass.
    assert sandbox._outputs_match("foo bar baz", "totally different") is False


def test_verify_python_two_sum_list_spacing_end_to_end():
    # End-to-end regression for the real recorded bug (A3): a solution that
    # prints Python's default `[0, 1]` list repr against a `[0,1]`-style
    # expected output must PASS, not FAIL.
    good = (
        "import ast, sys\n"
        "line = sys.stdin.readline()\n"
        "nums = ast.literal_eval(line.split('nums = ')[1].split(', target')[0])\n"
        "target = int(line.split('target = ')[1])\n"
        "seen = {}\n"
        "for i, n in enumerate(nums):\n"
        "    if target - n in seen:\n"
        "        print([seen[target - n], i])\n"
        "        break\n"
        "    seen[n] = i\n"
    )
    r = sandbox.verify_python(good, "nums = [2,7,11,15], target = 9\n", "[0,1]")
    assert r.status == "pass", r


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
    # audit_hook=False: this tests the tree-kill layer, so the grandchild must
    # be allowed to spawn (the C6 hook would otherwise refuse it up front).
    r = sandbox.verify_python(
        code, str(pidfile) + "\n", "whatever", timeout=2, audit_hook=False
    )
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
    """Spawning more processes than the job's active-process cap (16) must be
    stopped: once the job is full, CreateProcess is denied inside the child, so
    the fork bomb can never reach its all-spawned success marker.

    The cap fires down one of two timing-dependent paths, and both prove
    containment:
      * the child wins the race to its own handler -> ``except OSError`` ->
        ``sys.exit(7)`` -> ``error``; or
      * the job tears the contained child down first -> no marker, no clean
        exit 7 -> a non-``pass`` result (``fail`` on an empty capture, or
        ``error`` on a nonzero exit).
    The invariant that is NOT timing-dependent is the security one: the bomb
    never prints ``SPAWNED-ALL``, so the result is never ``pass``. With no cap
    all 32 spawns succeed, the marker prints, and the status IS ``pass``. That
    regression is what the assertion below would flunk. Either way the ~15
    sleepers that spawned before the wall are inside the job, so close_job's
    KILL_ON_JOB_CLOSE in verify_python's finally reaps them.

    (Asserting the exact exit-7 branch made this flaky: on a loaded machine the
    job-teardown path wins often enough to redden CI. `status != "pass"` plus
    the wall-clock bound is the honest, deterministic form of the same claim.)"""
    bomb = (
        "import subprocess, sys\n"
        "procs = []\n"
        "try:\n"
        "    for _ in range(32):\n"
        "        procs.append(subprocess.Popen(\n"
        "            [sys.executable, '-c', 'import time; time.sleep(20)']))\n"
        "except OSError:\n"
        "    sys.exit(7)\n"   # the cap said no: one of the two valid paths
        "print('SPAWNED-ALL')\n"
    )
    start = time.monotonic()
    # audit_hook=False: exercise the job's process cap itself, not the C6
    # hook (which would refuse the very first spawn).
    r = sandbox.verify_python(bomb, "", "SPAWNED-ALL", timeout=30, audit_hook=False)
    elapsed = time.monotonic() - start
    assert elapsed < 25, f"took {elapsed:.1f}s -- process cap did not stop the bomb"
    # The bomb was contained: it never spawned all 32 and printed the marker, so
    # the result is never `pass`. The exact non-pass shape (error/exit-7 vs a
    # torn-down fail) is a Windows Job Object timing detail, not the contract.
    assert r.status != "pass", r


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


def test_job_caps_unavailable_fails_closed_on_windows_only(monkeypatch):
    """SP3 review I1: on Windows a missing Job Object means the untrusted code
    would run with NO memory / process caps, so verification fails CLOSED
    (``not_verified``, never run) instead of silently degrading. POSIX never
    has a job (its caps are rlimits), so ``None`` there is the normal path."""
    monkeypatch.setattr(sandbox, "create_job_with_caps", lambda *a, **k: None)
    r = sandbox.verify_python(GOOD_DOUBLE, "21\n", "42")
    if os.name == "nt":
        assert r.status == "not_verified", r
        assert "sandbox caps unavailable" in r.note, r
    else:
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


def test_parse_samples_bare_input_body_keeps_its_own_indentation():
    """#11 regression: a bare-label Input body's OWN indentation can be
    meaningful (e.g. a nested grid row's formatting) and must survive
    verbatim — the old code ran every body line through the markdown-noise
    stripper, whose leading `[\\s...]+` half silently deleted leading
    whitespace on every line, not just markdown wrap characters."""
    text = (
        "Example 1:\n"
        "Input:\n"
        "grid = [\n"
        "  [1, 0, 0],\n"
        "  [0, 1, 0],\n"
        "]\n"
        "Output: [2,2]\n"
    )
    samples = sandbox.parse_samples(text)
    assert len(samples) == 1, samples
    lines = samples[0].stdin.splitlines()
    assert "  [1, 0, 0]," in lines, lines
    assert "  [0, 1, 0]," in lines, lines


def test_parse_samples_captures_output_on_following_line():
    """A bare ``Output:`` label with its value on the next line(s) must capture
    that value, not an empty expected_stdout (audit P1-1)."""
    samples = sandbox.parse_samples(MULTILINE_OUTPUT_PROBLEM)
    assert len(samples) == 1, f"expected the pair to be found, got {samples}"
    assert "root = [3,9,20,null,null,15,7]" in samples[0].stdin
    assert samples[0].expected_stdout == "[[3],[9,20],[15,7]]"


# --- A4: markdown emphasis around labels/values must not leak into I/O -----

BOLD_LABEL_PROBLEM = """\
Example 1:

**Input:** nums = [2,7,11,15], target = 9
**Output:** [0,1]
"""


def test_parse_samples_strips_bold_markdown_around_single_line_values():
    samples = sandbox.parse_samples(BOLD_LABEL_PROBLEM)
    assert len(samples) == 1, samples
    assert samples[0].stdin.strip() == "nums = [2,7,11,15], target = 9"
    assert samples[0].expected_stdout == "[0,1]"
    assert "*" not in samples[0].stdin
    assert "*" not in samples[0].expected_stdout


BOLD_MULTILINE_PROBLEM = """\
Example 1:

**Input:**
grid = [
  [1, 0],
  [0, 1]
]
**Output:**
**[2,2]**

Explanation: the diagonal sums to target.
"""


def test_parse_samples_strips_bold_markdown_around_multiline_body():
    samples = sandbox.parse_samples(BOLD_MULTILINE_PROBLEM)
    assert len(samples) == 1, samples
    assert samples[0].expected_stdout == "[2,2]"
    assert "*" not in samples[0].expected_stdout
    assert "grid" in samples[0].stdin
    assert "*" not in samples[0].stdin


def test_parse_samples_strips_backtick_wrapped_values():
    text = "Example 1:\nInput: `nums = [1,2], target = 3`\nOutput: `[0,1]`\n"
    samples = sandbox.parse_samples(text)
    assert len(samples) == 1, samples
    assert samples[0].stdin.strip() == "nums = [1,2], target = 3"
    assert samples[0].expected_stdout == "[0,1]"


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


# --- P2-2: mixed pass/error aggregation surfaces the error distinctly ------

def test_aggregation_mixed_pass_and_error_names_the_errored_count():
    """A run that both passes a sample AND crashes another must not be reported
    as a plain wrong-answer `fail` — the note has to name the errored count."""
    code = (
        "import sys\n"
        "n = int(sys.stdin.readline())\n"   # crashes on non-numeric stdin
        "print(n * 2)\n"
    )
    samples = [
        sandbox.Sample(stdin="21\n", expected_stdout="42"),   # -> pass
        sandbox.Sample(stdin="oops\n", expected_stdout="42"),  # -> error (ValueError)
    ]
    r = sandbox._verify_python_samples(code, samples, timeout=5)
    assert r.status == "fail", r
    assert r.samples_passed == 1
    assert "1 errored" in r.note, r.note


def test_aggregation_all_errored_is_a_pure_error():
    code = "import sys\nn = int(sys.stdin.readline())\nprint(n)\n"
    samples = [
        sandbox.Sample(stdin="a\n", expected_stdout="1"),  # error
        sandbox.Sample(stdin="b\n", expected_stdout="2"),  # error
    ]
    r = sandbox._verify_python_samples(code, samples, timeout=5)
    assert r.status == "error", r
    assert "2/2" in r.note, r.note


# --- B4: the per-sample error reason (timeout/crash note) survives ---------

def test_aggregation_keeps_the_timeout_note_per_sample():
    """A sample that times out must carry its OWN reason ("timed out after
    Xs") into the aggregated detail — the old code dropped the note entirely
    (r.detail was empty for a timeout), leaving "errored 1/1" with no why."""
    loop = "while True:\n    pass\n"
    samples = [sandbox.Sample(stdin="", expected_stdout="anything")]
    r = sandbox._verify_python_samples(code=loop, samples=samples, timeout=1)
    assert r.status == "error", r
    assert len(r.detail) == 1
    entry = r.detail[0]
    assert "timed out" in entry.get("note", "").lower(), entry
    # the sample's own stdin/expected are present too (#4: verify_python now
    # also returns a `detail` payload — with partial stdout/stderr — for a
    # timeout, not just for a crash/overflow).
    assert entry.get("expected") == "anything"
    assert "stdout" in entry


def test_aggregation_keeps_the_note_for_a_passing_sample_too():
    samples = [sandbox.Sample(stdin="21\n", expected_stdout="42")]
    r = sandbox._verify_python_samples(GOOD_DOUBLE, samples, timeout=5)
    assert r.status == "pass", r
    assert r.detail[0]["note"] == "output matched"


# --- P2-4: LEETCOACH_VERIFY_TIMEOUT knob -----------------------------------

def test_verify_timeout_env_knob_defaults_and_invalid_values_fall_back(monkeypatch):
    import config
    monkeypatch.delenv("LEETCOACH_VERIFY_TIMEOUT", raising=False)
    assert config.verify_timeout() == 10.0
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "not-a-number")
    assert config.verify_timeout() == 10.0
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "0")
    assert config.verify_timeout() == 10.0
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "-3")
    assert config.verify_timeout() == 10.0
    # "nan" parses as a float but must still fall back (NaN > 0 is False), so a
    # NaN knob can't silently disable the sample-verification containment timeout.
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "nan")
    assert config.verify_timeout() == 10.0
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "4")
    assert config.verify_timeout() == 4.0


# --- B5: timeouts are floats end-to-end; config clamps to (0, 86400] -------

def test_verify_timeout_rejects_inf_and_clamps_out_of_range(monkeypatch):
    import config
    # +inf/-inf must not disable the containment timeout (B5).
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "inf")
    assert config.verify_timeout() == 10.0
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "-inf")
    assert config.verify_timeout() == 10.0
    # far above a sane wall-clock ceiling (24h) CLAMPS to the ceiling (#6) —
    # it must not silently fall back to the (much shorter) default, which
    # would surprise a user who deliberately asked for a long budget.
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "999999")
    assert config.verify_timeout() == config.MAX_TIMEOUT_SECONDS
    # the ceiling itself is accepted.
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "86400")
    assert config.verify_timeout() == 86400.0
    # a genuine sub-second float is kept, not truncated.
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "0.5")
    assert config.verify_timeout() == 0.5
    # <=0 / NaN still reject to the default (#6 keeps this part of B5 intact).
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "0")
    assert config.verify_timeout() == 10.0
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "-3")
    assert config.verify_timeout() == 10.0
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "nan")
    assert config.verify_timeout() == 10.0


def test_run_timeout_rejects_inf_and_clamps_out_of_range(monkeypatch):
    import config
    monkeypatch.setenv("LEETCOACH_RUN_TIMEOUT", "inf")
    assert config.run_timeout() == 600.0
    # #6: clamped to the ceiling, not discarded to the default.
    monkeypatch.setenv("LEETCOACH_RUN_TIMEOUT", "999999")
    assert config.run_timeout() == config.MAX_TIMEOUT_SECONDS
    monkeypatch.setenv("LEETCOACH_RUN_TIMEOUT", "86400")
    assert config.run_timeout() == 86400.0


def test_verify_python_honours_a_sub_second_float_timeout():
    """A 0.5s budget must actually be ~0.5s, not truncated to 0 by an int()
    cast (the old `_verify_python_samples` passed `int(timeout)`, so 0.5
    silently became 0 and every verify with a sub-second budget mis-timed)."""
    loop = "while True:\n    pass\n"
    start = time.monotonic()
    r = sandbox.verify_python(loop, "", "anything", timeout=0.5)
    elapsed = time.monotonic() - start
    assert r.status == "error", r
    assert "timed out after 0.5s" in r.note, r.note
    assert elapsed < 5, f"took {elapsed:.1f}s -- sub-second timeout was not honoured"


def test_verify_python_samples_does_not_truncate_fractional_timeout():
    """`_verify_python_samples` must pass the float timeout straight through to
    `verify_python`, not `int(timeout)` (which turned 0.5 into 0)."""
    seen = []

    def spy_verify_python(code, stdin_text, expected_stdout, *, timeout, problem_text=""):
        seen.append(timeout)
        return sandbox.VerifyResult(status="pass", note="ok")

    orig = sandbox.verify_python
    sandbox.verify_python = spy_verify_python
    try:
        samples = [sandbox.Sample(stdin="", expected_stdout="x")]
        sandbox._verify_python_samples("print('x')\n", samples, timeout=0.5)
    finally:
        sandbox.verify_python = orig
    assert seen == [0.5], seen


def test_verify_answer_threads_the_configured_timeout(monkeypatch):
    """`verify_answer` must feed each sample-verify subprocess the configured
    timeout, not a hardcoded 10s — so the knob actually bounds runaway code."""
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "3")
    seen = []

    def spy_verify_python(code, stdin_text, expected_stdout, *, timeout, problem_text=""):
        seen.append(timeout)
        return sandbox.VerifyResult(status="pass", note="ok")

    monkeypatch.setattr(sandbox, "verify_python", spy_verify_python)
    r = sandbox.verify_answer("print('x')\n", SINGLE_SAMPLE_PROBLEM, "python")
    assert r.status == "pass", r
    assert seen and all(t == 3 for t in seen), seen


# --- #13: end-to-end integration, parsing.extract_code + sandbox ----------

_GUIDED_DOC_MULTI_BLOCK = (
    "Here's how hash maps work:\n\n"
    "```python\n"
    "# teaching snippet: hash map lookup demo, not the real solution\n"
    "d = {1: 'a'}\n"
    "print(d.get(1))\n"
    "```\n\n"
    "Now the full solution:\n\n"
    "```python\n"
    "import ast, sys\n"
    "\n"
    "def two_sum(nums, target):\n"
    "    seen = {}\n"
    "    for i, n in enumerate(nums):\n"
    "        complement = target - n\n"
    "        if complement in seen:\n"
    "            return [seen[complement], i]\n"
    "        seen[n] = i\n"
    "    return []\n"
    "\n"
    "if __name__ == \"__main__\":\n"
    "    line = sys.stdin.readline()\n"
    "    nums_part, target_part = line.split(', target')\n"
    "    nums = ast.literal_eval(nums_part.split('nums = ')[1])\n"
    "    target = int(target_part.split('=')[1])\n"
    "    print(two_sum(nums, target))\n"
    "```\n"
)

_GUIDED_DOC_PROBLEM_TEXT = (
    "Given an array of integers nums and an integer target, return indices of "
    "the two numbers that add up to target.\n\n"
    "Example 1:\n\n"
    "**Input:** nums = [2,7,11,15], target = 9\n"
    "**Output:** [0,1]\n"
)


def test_integrated_guided_multiblock_main_guard_doc_verifies_pass_real_python():
    """#13: end-to-end integration of `parsing.extract_code` + `sandbox` — a
    multi-block Guided-style doc (an earlier teaching snippet, then the real
    `__main__`-driven solution) whose solution prints Python's `[0, 1]` list
    repr against a LeetCode-style `**Input:**`/`**Output:**` `[0,1]`
    (no-space) example must verify PASS. Nothing in the sandbox is mocked —
    this runs a REAL python subprocess (only the doc text is a fixture)."""
    code = parsing.extract_code(_GUIDED_DOC_MULTI_BLOCK, "python")
    assert "teaching snippet" not in code
    assert "__main__" in code

    r = sandbox.verify_answer(code, _GUIDED_DOC_PROBLEM_TEXT, "python")
    assert r.status == "pass", r
    assert r.samples_total == 1
    assert r.samples_passed == 1


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
