"""Re-attempt "Test my code" (SP7 / D3).

The learner re-opens a saved problem with the answer hidden, writes their own
Python script and runs it in the SP3 sandbox (``sandbox.verify_python``: job
object fail-closed, audit hook, READY/go handshake, cancel event) against

* the sample I/O parsed from the problem's saved statement
  (``sandbox.parse_samples`` - the same parser Answer-mode verification uses),
* plus the learner's own cases (``input`` + optional ``expected``; with no
  expected output the case is just run and its output shown).

The script follows the same contract as a generated solution: it reads the
``Input:`` text on stdin (e.g. ``nums = [2,7,11,15], target = 9``) and prints
the result the way the ``Output:`` shows it (e.g. ``[0,1]``). Only Python
runs; C++ / Java answer "not supported yet". Nothing here is saved.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import config
import sandbox

CODE_CAP = 64 * 1024          # characters of attempt code
CASE_FIELD_CAP = 10_000       # characters per custom input / expected
MAX_CUSTOM_CASES = 10
MAX_SAMPLES = 10
ECHO_CAP = 8 * 1024           # characters of input/expected/output echoed back

STATUSES = ("pass", "fail", "error", "ran", "not_verified")


class CaseError(ValueError):
    """A custom case the request got wrong (-> 400 with this message)."""


@dataclass(frozen=True)
class Case:
    source: str               # "sample" | "custom"
    index: int                # 1-based within its source
    stdin: str
    expected: str | None      # None: run only, nothing to compare


def unsupported_message(language: str) -> str:
    label = {"cpp": "C++", "java": "Java"}.get(language, language)
    return (f"Running {label} code is not supported yet - only Python attempts can be "
            "tested here. Switch the language to Python, or ask for a Code Review.")


def parse_custom_cases(value) -> list[Case]:
    """Validate the request's ``cases`` list -> custom :class:`Case`\\ s.
    Raises :class:`CaseError` with a user-facing message."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise CaseError("Cases must be a list.")
    if len(value) > MAX_CUSTOM_CASES:
        raise CaseError(f"At most {MAX_CUSTOM_CASES} custom cases.")
    cases = []
    for i, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise CaseError(f"Case {i} must be an object with input and expected.")
        stdin = item.get("input")
        expected = item.get("expected")
        if not isinstance(stdin, str) or (expected is not None and not isinstance(expected, str)):
            raise CaseError(f"Case {i}: input and expected must be text.")
        if len(stdin) > CASE_FIELD_CAP or len(expected or "") > CASE_FIELD_CAP:
            raise CaseError(f"Case {i} is too long (max {CASE_FIELD_CAP} characters per field).")
        stdin = stdin.replace("\r\n", "\n")
        if not stdin.strip():
            raise CaseError(f"Case {i} needs an input.")
        expected = (expected or "").replace("\r\n", "\n").strip()
        cases.append(Case("custom", i, stdin.rstrip("\n") + "\n", expected or None))
    return cases


def sample_cases(statement: str) -> list[Case]:
    samples = sandbox.parse_samples(statement or "")[:MAX_SAMPLES]
    return [Case("sample", i, s.stdin, s.expected_stdout) for i, s in enumerate(samples, 1)]


def _clip(text) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= ECHO_CAP else text[:ECHO_CAP] + "\n… (truncated)"


def run_cases(code: str, cases: list[Case], *, problem_text: str = "",
              cancel: threading.Event | None = None, timeout: float | None = None,
              verify=None) -> dict:
    """Run ``code`` against each case (sequentially, each in a fresh sandbox
    process) and aggregate.

    Per case: ``pass`` / ``fail`` (output differed) / ``error`` (crash,
    timeout, output cap, blocked) / ``ran`` (a custom case with no expected
    output) / ``not_verified`` (the sandbox refused to run: caps unavailable,
    or cancelled - later cases are skipped). Overall: ``not_verified`` if
    nothing could run, else ``error`` when every compared case errored,
    ``fail`` when any compared case failed or errored, ``pass`` when all
    compared cases passed, ``ran`` when nothing was compared."""
    verify = verify or sandbox.verify_python
    timeout = config.verify_timeout() if timeout is None else timeout
    results = []
    stopped_note = ""
    for case in cases:
        if cancel is not None and cancel.is_set():
            stopped_note = "cancelled"
            break
        r = verify(code, case.stdin, case.expected or "", timeout=timeout,
                   problem_text=problem_text, cancel=cancel)
        status = getattr(r, "status", "error")
        note = getattr(r, "note", "") or ""
        if status == "not_verified":
            stopped_note = note or "the sandbox could not run the code"
            break
        detail = (getattr(r, "detail", None) or [{}])[0]
        if case.expected is None and status in ("pass", "fail"):
            status, note = "ran", "ran (no expected output to compare)"
        results.append({
            "source": case.source,
            "index": case.index,
            "status": status,
            "note": note,
            "input": _clip(case.stdin.rstrip("\n")),
            "expected": None if case.expected is None else _clip(case.expected),
            "got": _clip(detail.get("stdout", "")),
            "stderr": _clip(detail.get("stderr", "")),
            "returncode": detail.get("returncode"),
        })
    compared = [r for r in results if r["expected"] is not None or r["status"] == "error"]
    passed = sum(1 for r in compared if r["status"] == "pass")
    errored = sum(1 for r in compared if r["status"] == "error")
    if not results:
        status = "not_verified"
    elif not compared:
        status = "ran"
    elif passed == len(compared):
        status = "pass"
    elif errored == len(compared):
        status = "error"
    else:
        status = "fail"
    summary = f"{passed}/{len(compared)} passed" if compared else f"{len(results)} ran"
    if errored and status != "error":
        summary += f", {errored} errored"
    skipped = len(cases) - len(results)
    if skipped:
        summary += f"; {skipped} not run ({stopped_note})"
    return {
        "status": status,
        "summary": summary,
        "passed": passed,
        "total": len(compared),
        "ran": len(results),
        "skipped": skipped,
        "cases": results,
    }
