"""Best-effort sample-I/O verification of a generated solution (SP5).

The generated solution is **untrusted code**, so we run it the way STATlee runs
analysis code (``statlee/sandbox.py``):

* a throwaway working directory (``tempfile.mkdtemp``) cleaned in ``finally``;
* a **secret-free** environment (``_safe_env``) so the child can't read an API
  key or any other app secret — only the bare minimum Windows/CPython needs;
* POSIX ``resource`` rlimits where available (no-op on the Windows dev host);
* ``subprocess.run(..., capture_output=True, text=True, timeout=...)`` with
  output truncation.

The public surface:

* :func:`verify_python` — write Python to the throwaway dir, run it under
  ``sys.executable`` feeding ``stdin_text`` on stdin, diff stdout vs expected.
* :func:`parse_samples` — pull ``Input:`` / ``Output:`` example pairs out of a
  pasted LeetCode problem (best effort; ``[]`` when none found).
* :func:`verify_answer` — orchestrator. Python is first-class (parse samples ->
  run -> pass/fail). For cpp/java we only check a compiler is on PATH and
  otherwise return ``not_verified``. It **never raises** — a verifier hiccup
  must never break a study run.

Statuses (see :class:`VerifyResult`):

* ``"pass"``         — every parsed sample matched expected stdout.
* ``"fail"``         — at least one sample's stdout differed.
* ``"error"``        — the code crashed / timed out / wouldn't run.
* ``"not_verified"`` — couldn't verify (no samples, no compiler, unsupported
                       language) — *not* a failure, just "not auto-verified".
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

# Cap captured child output so a runaway print loop can't blow up memory / the
# saved markdown.
_OUTPUT_LIMIT = 64 * 1024


@dataclass
class Sample:
    """One example I/O pair pulled from a problem statement."""

    stdin: str
    expected_stdout: str


@dataclass
class VerifyResult:
    """The verdict for one ``verify_*`` call.

    ``status`` is one of ``pass`` / ``fail`` / ``error`` / ``not_verified``.
    ``note`` is a short human line (the reason for ``not_verified``/``error``,
    or a passed/failed-count summary). ``samples_total`` / ``samples_passed``
    let the caller show "2/3 samples passed". ``detail`` carries per-sample
    captured output for debugging / the saved ``.md``.
    """

    status: str = "not_verified"
    note: str = ""
    samples_total: int = 0
    samples_passed: int = 0
    detail: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "pass"


# --- secret-free environment (adapted from STATlee) ----------------------

def _safe_env(run_dir: str) -> dict:
    """A minimal, secret-free environment for the child process.

    The generated solution gets PATH (to find the interpreter) plus the bare
    Windows variables CPython needs to start, and nothing else — no
    ``ANTHROPIC_*`` / ``LEETCOACH_*`` / other secrets leak in.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": run_dir,
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
    }
    if os.name == "nt":
        # Windows dev host: these are plain paths (not secrets) that CPython
        # needs to locate its install and user-site packages. Mirrors STATlee.
        for key in (
            "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
            "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
        ):
            if key in os.environ:
                env[key] = os.environ[key]
        env["TEMP"] = env["TMP"] = run_dir
    return env


def _posix_limits():
    """A ``preexec_fn`` applying conservative rlimits. POSIX only; ``None`` on
    Windows (the dev host), matching STATlee."""
    if os.name == "nt":
        return None
    import resource  # noqa: PLC0415 - POSIX-only import

    def set_limits():
        mem_bytes = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        except (ValueError, OSError):
            pass  # not adjustable in some containers

    return set_limits


def _truncate(text: str) -> str:
    if text and len(text) > _OUTPUT_LIMIT:
        return text[:_OUTPUT_LIMIT] + f"\n... [truncated at {_OUTPUT_LIMIT // 1024} KB]"
    return text or ""


def _normalize(text: str) -> str:
    """Normalize for comparison: unify line endings, strip trailing whitespace on
    each line, and strip surrounding blank lines. LeetCode stdout diffs shouldn't
    fail on a trailing newline or CRLF mismatch."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


# --- run one Python snippet against fixed I/O ----------------------------

def verify_python(
    code: str,
    stdin_text: str,
    expected_stdout: str,
    *,
    timeout: int = 10,
) -> VerifyResult:
    """Run ``code`` under ``sys.executable``, feeding ``stdin_text`` on stdin,
    and compare captured stdout to ``expected_stdout`` (whitespace-normalized).

    Returns a :class:`VerifyResult` with status ``pass`` / ``fail`` / ``error``.
    Never raises.
    """
    run_dir = tempfile.mkdtemp(prefix="leetcoach_run_")
    try:
        script_path = os.path.join(run_dir, "solution.py")
        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(code or "")
        except OSError as exc:
            return VerifyResult(status="error", note=f"could not write script: {exc}")

        popen_kwargs = {"cwd": run_dir, "env": _safe_env(run_dir)}
        preexec = _posix_limits()
        if preexec:
            popen_kwargs["preexec_fn"] = preexec

        try:
            proc = subprocess.run(
                [sys.executable, script_path],
                input=stdin_text or "",
                capture_output=True,
                text=True,
                timeout=timeout,
                **popen_kwargs,
            )
        except subprocess.TimeoutExpired:
            return VerifyResult(
                status="error",
                note=f"timed out after {timeout}s",
            )
        except (OSError, ValueError) as exc:
            return VerifyResult(status="error", note=f"could not run: {exc}")

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        if proc.returncode != 0:
            # A crash / nonzero exit is an `error`, not a content `fail`.
            return VerifyResult(
                status="error",
                note=f"exited with code {proc.returncode}",
                detail=[{
                    "stdin": stdin_text,
                    "expected": expected_stdout,
                    "stdout": _truncate(stdout),
                    "stderr": _truncate(stderr),
                    "returncode": proc.returncode,
                }],
            )

        got = _normalize(stdout)
        want = _normalize(expected_stdout)
        ok = got == want
        return VerifyResult(
            status="pass" if ok else "fail",
            note="output matched" if ok else "output differed",
            samples_total=1,
            samples_passed=1 if ok else 0,
            detail=[{
                "stdin": stdin_text,
                "expected": expected_stdout,
                "stdout": _truncate(stdout),
                "stderr": _truncate(stderr),
                "match": ok,
            }],
        )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


# --- pull sample I/O out of a pasted problem -----------------------------

# Matches the common LeetCode layout:
#     Input: nums = [2,7,11,15], target = 9
#     Output: [0,1]
# We capture everything after Input:/Output: up to the next label or a blank
# line. ``Explanation:`` (and the next ``Example``/``Constraints``) terminate the
# Output capture so we don't swallow prose.
_INPUT_RE = re.compile(
    r"(?im)^[ \t>*-]*input\s*[:=]\s*(.*?)\s*$"
)
_OUTPUT_RE = re.compile(
    r"(?im)^[ \t>*-]*output\s*[:=]\s*(.*?)\s*$"
)
# Section markers that terminate the search for an ``Output:`` after an
# ``Input:``. A multi-line Input block (e.g. an array printed across several
# lines) can push Output well past a small fixed window, so instead of a fixed
# line count we scan forward until the Output — or bail at the next section so
# we never swallow prose or the following example's data.
_TERMINAL_RE = re.compile(
    r"(?im)^[ \t>*-]*(?:explanation|example|constraints?|follow[ -]?up|note)\b"
)


def parse_samples(problem_text: str) -> list:
    """Best-effort extraction of ``[Sample(stdin, expected_stdout), ...]`` from a
    pasted LeetCode-style problem.

    Strategy: walk the text line by line. When we hit an ``Input:`` line, take its
    remainder as the stdin; the next ``Output:`` line's remainder is the expected
    stdout. This handles the standard "Example N:" / "Input: ... Output: ..."
    layout. Returns ``[]`` when no pair is found (caller marks "not auto-verified").

    The stdin we synthesize is the *raw text after ``Input:``* (e.g.
    ``nums = [2,7,11,15], target = 9``) on its own line, and expected stdout is
    the raw text after ``Output:`` (e.g. ``[0,1]``). The generated Python driver
    is instructed (see ``prompts.py``) to read that exact line format from stdin
    and print the result in that exact format — so this is a literal round-trip.

    Bare labels (data on the following lines) are handled too: an empty
    remainder after ``Input:`` takes the lines up to the ``Output:`` as the
    stdin body (verbatim, surrounding blank lines trimmed), and an empty
    remainder after ``Output:`` takes the following lines up to a blank line /
    the next section / the next ``Input:`` as the expected stdout. A pair where
    either side is *still* empty is dropped — a bogus ``('\\n', '')`` sample
    would false-FAIL a correct solution, whereas no sample degrades to
    "not auto-verified".
    """
    if not problem_text:
        return []

    samples: list = []
    lines = problem_text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        m_in = _INPUT_RE.match(lines[i])
        if not m_in:
            i += 1
            continue
        stdin_val = m_in.group(1).strip()
        # Scan forward for the matching Output:. No fixed window — a multi-line
        # Input block can push Output arbitrarily far down — but bail at a
        # section marker (Explanation/Example/Constraints/...) or another Input:,
        # so we never cross into prose or the next example.
        out_val = None
        j = i + 1
        while j < n:
            m_out = _OUTPUT_RE.match(lines[j])
            if m_out:
                out_val = m_out.group(1).strip()
                break
            # Stop at the next section or another Input: before an Output:
            # (malformed / unpaired Input).
            if _INPUT_RE.match(lines[j]) or _TERMINAL_RE.match(lines[j]):
                break
            j += 1
        if out_val is None:
            i += 1
            continue

        # Bare ``Input:`` label — the data sits on the lines between it and the
        # ``Output:``. Take that body verbatim (indentation may be meaningful),
        # trimming surrounding blank lines.
        if not stdin_val:
            body = lines[i + 1:j]
            while body and not body[0].strip():
                body.pop(0)
            while body and not body[-1].strip():
                body.pop()
            stdin_val = "\n".join(body)

        # Bare ``Output:`` label — the value sits on the following line(s), up
        # to a blank line, the next section, or the next ``Input:``. Each line
        # is stripped, mirroring the single-line ``.strip()`` convention.
        next_i = j + 1
        if not out_val:
            out_body = []
            k = j + 1
            while k < n:
                line = lines[k]
                if (not line.strip()
                        or _TERMINAL_RE.match(line)
                        or _INPUT_RE.match(line)):
                    break
                out_body.append(line.strip())
                k += 1
            out_val = "\n".join(out_body)
            next_i = k

        if stdin_val and out_val:
            samples.append(Sample(stdin=stdin_val + "\n", expected_stdout=out_val))
        # else: even after the multi-line capture one side is still empty —
        # skip the pair rather than emit a bogus sample. Either way, resume
        # past everything this pair consumed (no Input: line is in that span;
        # both scans bail at _INPUT_RE).
        i = next_i
    return samples


# --- top-level orchestrator ----------------------------------------------

# Compilers we'd need for non-Python verification. We only *probe* for them;
# actually compiling/running cpp/java is out of MVP scope (shown not-verified).
_COMPILERS = {
    "cpp": ("g++", "C++"),
    "java": ("javac", "Java"),
}


def verify_answer(code: str, problem_text: str, language: str) -> VerifyResult:
    """Verify a generated solution against the problem's sample I/O.

    * **python** — first-class: parse samples from ``problem_text``, run ``code``
      against each, and aggregate to ``pass`` (all matched) / ``fail`` (any
      differed) / ``error`` (a sample crashed). If no samples parse, status is
      ``not_verified`` ("no sample I/O found").
    * **cpp / java** — only checks the compiler is on PATH (``g++`` / ``javac``).
      Absent -> ``not_verified`` ("no <compiler> on PATH..."). Present but
      auto-run is still out of MVP scope -> ``not_verified`` (compiler found, but
      auto-run unsupported). Either way it's "not auto-verified", never a fail.

    Never raises: any unexpected failure degrades to ``not_verified`` so a
    verifier bug can't break the study run.
    """
    try:
        lang = (language or "").strip().lower()

        if lang in ("python", "py", "python3"):
            if not code or not code.strip():
                return VerifyResult(status="not_verified", note="no code to verify")
            samples = parse_samples(problem_text)
            if not samples:
                return VerifyResult(
                    status="not_verified", note="no sample I/O found in problem"
                )
            return _verify_python_samples(code, samples)

        if lang in _COMPILERS:
            compiler, label = _COMPILERS[lang]
            if shutil.which(compiler) is None:
                return VerifyResult(
                    status="not_verified",
                    note=(
                        f"no {label} compiler ({compiler}) on PATH — "
                        "auto-verification skipped"
                    ),
                )
            # Compiler present, but compiling/running cpp/java is out of MVP scope.
            return VerifyResult(
                status="not_verified",
                note=(
                    f"{label} compiler found, but {label} auto-run is not "
                    "supported yet — verify manually"
                ),
            )

        return VerifyResult(
            status="not_verified", note=f"unsupported language {language!r}"
        )
    except Exception as exc:  # noqa: BLE001 - verifier must never break a run
        return VerifyResult(status="not_verified", note=f"verifier error: {exc}")


def _verify_python_samples(code: str, samples: list) -> VerifyResult:
    """Run ``code`` against each parsed sample and aggregate the verdict."""
    total = len(samples)
    passed = 0
    detail: list = []
    saw_error = False
    for idx, s in enumerate(samples, start=1):
        r = verify_python(code, s.stdin, s.expected_stdout)
        entry = {"sample": idx, "status": r.status}
        if r.detail:
            entry.update(r.detail[0])
        detail.append(entry)
        if r.status == "pass":
            passed += 1
        elif r.status == "error":
            saw_error = True

    if passed == total:
        status, note = "pass", f"all {total} sample(s) passed"
    elif saw_error and passed == 0:
        status, note = "error", "code errored on sample input"
    else:
        status, note = "fail", f"{passed}/{total} sample(s) passed"

    return VerifyResult(
        status=status,
        note=note,
        samples_total=total,
        samples_passed=passed,
        detail=detail,
    )
