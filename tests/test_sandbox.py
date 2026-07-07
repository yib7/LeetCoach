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
    verification degraded to 'not auto-verified')."""
    samples = sandbox.parse_samples(MULTILINE_INPUT_PROBLEM)
    assert len(samples) == 1, f"expected the pair to be found, got {samples}"
    assert samples[0].expected_stdout == "[2,2]"


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
