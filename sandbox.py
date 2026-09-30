"""Best-effort sample-I/O verification of a generated solution (SP5).

The generated solution is **untrusted code**, so we run it the way STATlee runs
analysis code (``statlee/sandbox.py``):

* a throwaway working directory (``tempfile.mkdtemp``) cleaned in ``finally``;
* a **secret-free** environment (``_safe_env``) so the child can't read an API
  key or any other app secret — only the bare minimum Windows/CPython needs;
* POSIX ``resource`` rlimits where available; on Windows a **Job Object**
  (``proc_util.create_job_with_caps`` created+configured *before* the spawn,
  ``assign_to_job`` right after) caps per-process memory (512 MB, parity with
  ``RLIMIT_AS``) and active process count (16), with KILL_ON_JOB_CLOSE so
  closing the job handle in the ``finally`` nukes any straggler;
* a **trusted bootstrap** (``sandbox_bootstrap.py``, SP3 A5) is what the child
  actually runs, under the REAL interpreter (``sys._base_executable -I``, never
  the venv launcher stub). It blocks on a one-byte handshake that the parent
  sends only after the job is assigned, so no untrusted line can run outside
  the caps, however long a GIL-starved parent takes to assign it;
* an **audit hook** installed by that bootstrap (C6) refuses writes outside the
  run dir, reads of known secret paths (:func:`_secret_paths`), sockets,
  process creation and ctypes loading — defence in depth, NOT a security
  boundary (SECURITY.md says so);
* ``subprocess.Popen`` with both output pipes drained on capped reader threads
  (never more than ``_OUTPUT_LIMIT`` retained — a runaway print loop gets the
  child killed, not hundreds of MB buffered) and a **whole-tree kill** on
  timeout/overflow (``proc_util.kill_process_tree``) so grandchildren spawned
  by the untrusted code die too.

The public surface:

* :func:`verify_python` — write Python to the throwaway dir, run it via the
  bootstrap feeding ``stdin_text`` on stdin, diff stdout vs expected.
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

import ast
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

import config
from proc_util import (
    assign_to_job,
    close_job,
    create_job_with_caps,
    kill_process_tree,
    terminate_job,
)

# Cap captured child output so a runaway print loop can't blow up memory / the
# saved markdown. Enforced *while reading* (see _CappedReader): the child is
# killed as soon as either stream exceeds it, not merely trimmed afterwards.
_OUTPUT_LIMIT = 64 * 1024

# Each throwaway run dir is ``<tempdir>/leetcoach_run_<random>``. The startup
# sweep (B6) only ever touches directories carrying this exact prefix.
_RUN_DIR_PREFIX = "leetcoach_run_"
_STALE_RUN_DIR_AGE_S = 24 * 60 * 60

# Indirection so tests can observe/skip the rmtree backoff without sleeping.
_sleep = time.sleep

# Per-process memory cap for the untrusted child: RLIMIT_AS on POSIX, a Job
# Object ProcessMemoryLimit on Windows — same number, parity by construction.
_MEM_LIMIT_BYTES = 512 * 1024 * 1024

# Windows job active-process cap. Deliberately tighter than the POSIX
# RLIMIT_NPROC (64): NPROC counts EVERY process of the user so it has to be
# generous, while the job counts only this child's own tree — a legitimate
# solution needs 1 process (maybe a few for multiprocessing), so 16 is ample
# headroom and stops a fork bomb almost immediately.
_JOB_PROCESS_CAP = 16

# A5: the trusted bootstrap the child runs first (see sandbox_bootstrap.py). It
# blocks on a one-byte handshake until the parent has assigned the job object,
# then runs the solution. The go byte is sent as the first byte of the child's
# stdin, immediately followed by the sample input.
_BOOTSTRAP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sandbox_bootstrap.py"
)
_GO = b""

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def _secret_paths() -> list:
    """Paths the C6 audit hook refuses to open or list (defence in depth).

    Deliberately short: the Claude Code login/config (``~/.claude``,
    ``~/.claude.json``, ``$CLAUDE_CONFIG_DIR``), this repo's ``.env`` (plus the
    one ``LEETCOACH_DOTENV_PATH`` points at), SSH / cloud / git / GitHub CLI
    credentials, and the Windows credential stores. Only paths are computed
    here — nothing is ever opened.
    """
    home = os.path.expanduser("~")
    paths = [
        os.path.join(home, ".claude"),
        os.path.join(home, ".claude.json"),
        os.path.join(home, ".ssh"),
        os.path.join(home, ".aws"),
        os.path.join(home, ".git-credentials"),
        os.path.join(home, ".netrc"),
        os.path.join(home, ".config", "gh"),
        os.path.join(_REPO_DIR, ".env"),
    ]
    for var in ("CLAUDE_CONFIG_DIR", "LEETCOACH_DOTENV_PATH"):
        if os.environ.get(var):
            paths.append(os.environ[var])
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths += [
            os.path.join(appdata, "Microsoft", "Credentials"),
            os.path.join(appdata, "Microsoft", "Protect"),
            os.path.join(appdata, "GitHub CLI"),
        ]
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        paths.append(os.path.join(localappdata, "Microsoft", "Credentials"))
    return paths


def _child_python() -> str:
    """The interpreter the untrusted child runs under (A5).

    ``sys._base_executable`` — the REAL interpreter — rather than
    ``sys.executable``: inside a Windows venv the latter is a launcher stub
    that re-spawns the real ``python.exe`` as its own child, and that
    grandchild can be created before the parent assigns the stub to the job
    object, i.e. entirely outside the memory / process caps. Falls back to
    ``sys.executable`` when there is no distinct base (not a venv, or an
    embedded interpreter without the attribute / file).
    """
    base = getattr(sys, "_base_executable", None)
    if base and os.path.isfile(base):
        return base
    return sys.executable


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
        resource.setrlimit(resource.RLIMIT_AS, (_MEM_LIMIT_BYTES,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        except (ValueError, OSError):
            pass  # not adjustable in some containers

    return set_limits


class _CappedReader(threading.Thread):
    """Drain one child pipe on a daemon thread, retaining at most
    ``_OUTPUT_LIMIT`` bytes.

    B6: reads are RAW (``os.read`` on the pipe's fd), returning whatever has
    arrived instead of waiting for a full buffer. The old buffered
    ``read(8192)`` blocked until 8 KB or EOF, so when a grandchild held the
    pipe open after the solution exited, a short answer sat in the reader's
    private buffer, the bounded join gave up, and the verdict saw empty
    stdout. Bytes are decoded once, at the end (:meth:`text`).

    Draining both pipes on dedicated threads is what makes the design
    deadlock-free: the child can never block on a full stdout/stderr OS buffer
    while the parent blocks on the other pipe (the classic two-pipe deadlock).
    Past the cap the thread keeps *draining* (so the child isn't wedged on a
    full pipe) but stops *retaining*, and flags ``overflowed`` so the caller
    can kill the process tree promptly instead of buffering hundreds of MB.
    """

    def __init__(self, stream) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._chunks: list = []
        self._kept = 0
        self._total = 0
        self._lock = threading.Lock()
        self.overflowed = threading.Event()
        self.start()

    def run(self) -> None:  # noqa: D102 - thread body
        try:
            fd = self._stream.fileno()
            while True:
                data = os.read(fd, 65536)
                if not data:
                    break  # EOF: every write handle to the pipe is closed
                with self._lock:
                    self._total += len(data)
                    if self._kept < _OUTPUT_LIMIT:
                        piece = data[: _OUTPUT_LIMIT - self._kept]
                        self._chunks.append(piece)
                        self._kept += len(piece)
                if self._total > _OUTPUT_LIMIT:
                    self.overflowed.set()
        except (OSError, ValueError):
            pass  # pipe torn down under us mid-kill — keep what we have
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    def text(self) -> str:
        """The retained output decoded once at the end (a multi-byte character
        split across reads or by the cap can't raise), with a truncation
        marker if any was dropped."""
        with self._lock:  # the thread may still be draining a held pipe
            raw, total, kept = b"".join(self._chunks), self._total, self._kept
        joined = raw.decode("utf-8", errors="replace")
        if total > kept:
            joined += f"\n... [truncated at {_OUTPUT_LIMIT // 1024} KB]"
        return joined


def _clear_readonly_and_retry(func, path, exc) -> None:
    """``shutil.rmtree`` ``onexc`` hook: a solution may have left a read-only
    file in its run dir, which Windows refuses to delete. Clear the bit and
    retry once; anything else re-raises into the retry loop."""
    if isinstance(exc, PermissionError):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
            return
        except OSError:
            pass
    raise exc


def _rmtree_with_retry(path: str, *, attempts: int = 6, base_delay: float = 0.05) -> bool:
    """Delete ``path`` with exponential backoff (B6). Returns True once it is
    gone, False if it outlived every attempt. Never raises.

    Right after a kill, Windows can keep a handle on a file in the run dir for
    a moment (the process object is torn down asynchronously), so the old
    single ``rmtree(ignore_errors=True)`` routinely leaked the dir: 98 stale
    ``leetcoach_run_*`` dirs were found in %TEMP%. Worst case this waits
    ~1.5 s in total (0.05 + 0.1 + ... + 0.8).
    """
    delay = base_delay
    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onexc=_clear_readonly_and_retry)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if not os.path.lexists(path):
                return True
            if attempt == attempts - 1:
                break
            _sleep(delay)
            delay *= 2
    return False


def sweep_stale_run_dirs(
    *, tmp_root: str | None = None, max_age_s: float = _STALE_RUN_DIR_AGE_S
) -> int:
    """Startup housekeeping (B6): delete ``leetcoach_run_*`` DIRECTORIES in the
    temp dir whose mtime is older than ``max_age_s`` (default one day), i.e.
    dirs a crashed or killed app left behind. Returns how many were removed.

    Deliberately narrow: only real directories whose name starts with
    :data:`_RUN_DIR_PREFIX`; files, symlinks and junctions carrying the prefix
    are skipped (never followed), and nothing else in the temp dir is ever
    touched. Never raises.
    """
    root = tmp_root or tempfile.gettempdir()
    cutoff = time.time() - max_age_s
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(_RUN_DIR_PREFIX):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            if os.path.isjunction(entry.path):
                continue
            if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if _rmtree_with_retry(entry.path, attempts=2):
            removed += 1
    return removed


def _feed_stdin(stdin_pipe, data: bytes) -> None:
    """Write ``data`` (go byte + sample input) to the child's stdin and close it
    (own daemon thread: a child that never reads stdin must not deadlock the
    parent's write)."""
    try:
        stdin_pipe.write(data)
    except (BrokenPipeError, OSError, ValueError):
        pass  # child exited / closed stdin without reading — not an error
    finally:
        try:
            stdin_pipe.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


def _normalize(text: str) -> str:
    """Normalize for comparison: unify line endings, strip trailing whitespace on
    each line, and strip surrounding blank lines. LeetCode stdout diffs shouldn't
    fail on a trailing newline or CRLF mismatch."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


# --- A3: tolerant structural output comparison ----------------------------
#
# The old comparison was ``_normalize(got) == _normalize(want)`` — whitespace
# trimming only, so `[0, 1]` vs `[0,1]`, `True` vs `true`, and `'x'` vs `"x"`
# all FAILED a correct solution (a real recorded Two Sum answer was graded
# FAIL 0/3 for exactly this reason). :func:`_outputs_match` adds a tolerant
# second tier that parses both sides as a structured value (Python-literal or
# JSON-ish) and compares that structure, with a float tolerance. It stays
# conservative by design: order-insensitivity is applied ONLY when the
# problem text explicitly says "any order" AND the two sides are the same
# multiset — a genuinely wrong (misordered when order matters, or a different
# value entirely) answer must never become a PASS.

_JSON_KEYWORD_MAP = {"true": "True", "false": "False", "null": "None"}

# Matches EITHER a full quoted string (single or double, with backslash
# escapes honoured) OR a bare JSON keyword. Quoted-string alternatives are
# listed FIRST so the regex engine consumes an entire string literal in one
# match — a `true`/`false`/`null` substring INSIDE quotes is swallowed as part
# of that match and never reaches the bare-keyword alternatives. Without this,
# a naive `\bnull\b` substitution would rewrite the "null" inside the string
# `"not null"` into `"not None"`, silently making two DIFFERENT string values
# compare equal (a false PASS).
_JSON_TOKEN_RE = re.compile(
    r"'[^'\\]*(?:\\.[^'\\]*)*'"
    r'|"[^"\\]*(?:\\.[^"\\]*)*"'
    r"|\btrue\b|\bfalse\b|\bnull\b"
)


def _translate_json_keywords(text: str) -> str:
    """Rewrite bare JSON ``true``/``false``/``null`` to Python's
    ``True``/``False``/``None`` — but never inside a quoted string, so a
    keyword-shaped SUBSTRING of an actual string value is left alone (see
    :data:`_JSON_TOKEN_RE`)."""

    def repl(m: re.Match) -> str:
        tok = m.group(0)
        if tok[0] in "'\"":
            return tok  # a whole quoted string literal: leave verbatim
        return _JSON_KEYWORD_MAP[tok]

    return _JSON_TOKEN_RE.sub(repl, text)


def _try_parse_structured(text: str):
    """Best-effort parse of ``text`` as a Python-literal/JSON-ish value (list,
    tuple, dict, number, bool, ``None``, or string). Returns ``(True, value)``
    on success, ``(False, None)`` if nothing sensible could be parsed. Never
    raises."""
    s = (text or "").strip()
    if not s:
        return False, None

    candidates = [s]
    # JSON spells booleans/null lowercase; Python's ast needs the capitalized
    # spelling, so also try a translated candidate (`true` -> `True`, etc.),
    # skipping any occurrence inside a quoted string (see
    # :func:`_translate_json_keywords`).
    translated = _translate_json_keywords(s)
    if translated != s:
        candidates.append(translated)

    for candidate in candidates:
        try:
            return True, ast.literal_eval(candidate)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue

    # Last resort: a bare, unquoted word/phrase with no literal syntax at all
    # is treated as the string it visually is, so an unquoted `hello` matches
    # a quoted `"hello"`. This can never produce a false PASS beyond what the
    # tier-1 exact match already would: with no other structure to compare,
    # it degrades to plain string equality of the trimmed text.
    try:
        return True, ast.literal_eval(repr(s))
    except (ValueError, SyntaxError, TypeError):
        return False, None


def _values_equal(a, b, *, rel_tol: float = 1e-5, abs_tol: float = 1e-9) -> bool:
    """Structural equality with a float tolerance. ``bool`` is checked before
    the numeric branch (``bool`` is a subclass of ``int`` in Python) so
    ``True`` never equals ``1``.

    The float tolerance is applied ONLY when at least one side is a ``float``
    — two ``int``s must compare EXACTLY equal. Regression: ``math.isclose``'s
    default ``rel_tol=1e-5`` is scaled by the operands' magnitude, so two
    large-but-different ints (``100001`` vs ``100000``, or two ~1e9 values a
    LeetCode "wrong answer" would plausibly print) were being reported equal
    even though no float was involved anywhere.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_values_equal(x, y, rel_tol=rel_tol, abs_tol=abs_tol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_values_equal(a[k], b[k], rel_tol=rel_tol, abs_tol=abs_tol) for k in a)
    return a == b


def _any_order_allowed(problem_text: str) -> bool:
    """True only when the problem statement explicitly says the order of a
    returned collection doesn't matter — the sole trigger for order-
    insensitive comparison (A3 is deliberately conservative here)."""
    return "any order" in (problem_text or "").lower()


def _multiset_equal(a, b) -> bool:
    """True iff sequences ``a``/``b`` hold the same elements irrespective of
    order (each element matched at most once, via :func:`_values_equal`)."""
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        return False
    if len(a) != len(b):
        return False
    remaining = list(b)
    for item in a:
        for i, candidate in enumerate(remaining):
            if _values_equal(item, candidate):
                remaining.pop(i)
                break
        else:
            return False
    return True


def _outputs_match(got: str, want: str, problem_text: str = "") -> bool:
    """The verifier's comparison: exact-normalized first, then a tolerant
    structural comparison, then (only if the problem allows it) order-
    insensitive. Never raises."""
    # Tier 1: exact, whitespace-normalized match — the fast, zero-risk path.
    if _normalize(got) == _normalize(want):
        return True

    # Tier 2: tolerant structural comparison (JSON/Python-literal aware).
    ok_got, val_got = _try_parse_structured(got)
    ok_want, val_want = _try_parse_structured(want)
    if not (ok_got and ok_want):
        return False  # couldn't parse structurally on at least one side
    if _values_equal(val_got, val_want):
        return True

    # Tier 3: order-insensitive, ONLY when the problem says so AND the two
    # sides are the same multiset — never turns a genuinely wrong answer (a
    # different value, or a misordered one when order matters) into a pass.
    return _any_order_allowed(problem_text) and _multiset_equal(val_got, val_want)


# --- run one Python snippet against fixed I/O ----------------------------

def verify_python(
    code: str,
    stdin_text: str,
    expected_stdout: str,
    *,
    timeout: float = 10.0,
    problem_text: str = "",
    audit_hook: bool = True,
) -> VerifyResult:
    """Run ``code`` through the trusted bootstrap (A5; see
    :func:`_child_python`), feeding ``stdin_text`` on stdin,
    and compare captured stdout to ``expected_stdout`` via :func:`_outputs_match`
    (exact-normalized, then tolerant structural, then — only when ``problem_text``
    says "any order" — order-insensitive; see A3).

    Returns a :class:`VerifyResult` with status ``pass`` / ``fail`` / ``error``.
    Never raises. ``timeout`` is a float end-to-end (B5): a sub-second budget
    like ``0.5`` is honoured rather than truncated to ``0``.

    Containment (the code is untrusted): on timeout the whole process TREE is
    killed (grandchildren included), and each output stream is capped at
    ``_OUTPUT_LIMIT`` — exceeding it kills the tree and yields ``error``
    ("output exceeded ... limit") since the capture is incomplete. On Windows
    the child also runs inside a Job Object capping memory and process count
    (best effort — a job API failure degrades to an uncapped run, never an
    error); POSIX gets the equivalent rlimits via ``preexec_fn``.

    ``audit_hook=False`` skips the C6 audit hook. It exists ONLY so tests can
    exercise the other containment layers (job caps, tree kill, grandchild
    pipe capture) by spawning a grandchild on purpose; production callers
    never pass it.
    """
    run_dir = tempfile.mkdtemp(prefix=_RUN_DIR_PREFIX)
    job_handle = None
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
        if os.name != "nt":
            # Own session => own process group, so a whole-tree kill can
            # reach grandchildren via killpg (C5 parity with the claude runner).
            popen_kwargs["start_new_session"] = True

        # Windows: create + configure the Job Object BEFORE the spawn. Every
        # slow step (one-time ctypes setup at proc_util import, job creation,
        # limit configuration) happens while no child exists — an earlier
        # revision did the ctypes setup lazily AFTER Popen, and the ~100ms
        # first-call import cost let the first child of a fresh process run
        # its untrusted code before the caps existed (cold-start race).
        # None on POSIX or if the job API failed -> run uncapped; caps are
        # defense-in-depth and must never break a verification run.
        job_handle = create_job_with_caps(
            memory_bytes=_MEM_LIMIT_BYTES,
            active_processes=_JOB_PROCESS_CAP,
        )

        # A5: the child is the REAL interpreter in isolated mode (-I: no user
        # site, no PYTHON* env, script dir off sys.path; -X utf8: UTF-8 stdio
        # regardless of the console code page; -B: no .pyc writes) running the
        # trusted bootstrap, which blocks until we release it below. Binary
        # pipes: output is read raw and decoded once at the end (B6).
        argv = [
            _child_python(), "-I", "-X", "utf8", "-B",
            _BOOTSTRAP_PATH, script_path, run_dir,
            json.dumps({
                "run_dir": run_dir,
                "audit": bool(audit_hook),
                "secret_paths": _secret_paths(),
            }),
        ]
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **popen_kwargs,
            )
        except (OSError, ValueError) as exc:
            return VerifyResult(status="error", note=f"could not run: {exc}")

        # A5 ordering: assign the job FIRST, and only then release the go byte.
        # Until the bootstrap reads it, no untrusted line has run — so it no
        # longer matters how long this thread takes to get here (the old
        # "one syscall beats ~20ms of interpreter startup" argument lost to GIL
        # contention: a busy parent let a 700 MB allocation escape 19/20 runs).
        assign_to_job(job_handle, proc)

        threading.Thread(
            target=_feed_stdin,
            args=(proc.stdin, _GO + (stdin_text or "").encode("utf-8")),
            daemon=True,
        ).start()
        out_reader = _CappedReader(proc.stdout)
        err_reader = _CappedReader(proc.stderr)

        # Wait for exit / timeout / output overflow — whichever comes first.
        # A short poll loop (not proc.wait(timeout)) so the overflow flag can
        # interrupt the wait; 50ms granularity is plenty for a verifier.
        deadline = time.monotonic() + timeout
        timed_out = False
        while proc.poll() is None:
            if out_reader.overflowed.is_set() or err_reader.overflowed.is_set():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)

        if proc.poll() is None:
            # Timeout or overflow: kill the WHOLE tree — the untrusted code may
            # have spawned grandchildren that a plain terminate() would leak —
            # then reap the direct child (kill() as a last resort).
            kill_process_tree(proc, group=os.name != "nt")
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # unreapable zombie — readers are daemons, move on

        # B6: the direct child is gone, but a straggler it spawned may still
        # hold a pipe open, so the readers would never see EOF. Give them a
        # moment, then kill whatever is left (the whole job on Windows, the
        # process group on POSIX) so they drain and finish promptly. Raw reads
        # mean everything the solution printed is already captured anyway.
        out_reader.join(timeout=0.2)
        err_reader.join(timeout=0.2)
        if out_reader.is_alive() or err_reader.is_alive():
            if not terminate_job(job_handle) and os.name != "nt":
                kill_process_tree(proc, group=True)

        # Bounded join: if a leaked write handle keeps a pipe open the daemon
        # readers may never see EOF, and we must not hang on them.
        out_reader.join(timeout=2)
        err_reader.join(timeout=2)

        # Whatever the readers captured before the kill is still useful context
        # for the user (and it's already there — the readers keep draining
        # right up to the kill) — grab it regardless of the timed-out branch
        # below (B4/#4: a timeout used to report bare "timed out after Xs"
        # with no stdin/expected/partial-output, which the saved .md then
        # couldn't show either).
        stdout = out_reader.text()
        stderr = err_reader.text()

        if timed_out:
            return VerifyResult(
                status="error",
                note=f"timed out after {timeout}s",
                detail=[{
                    "stdin": stdin_text,
                    "expected": expected_stdout,
                    "stdout": stdout,
                    "stderr": stderr,
                }],
            )

        if out_reader.overflowed.is_set() or err_reader.overflowed.is_set():
            # Applies whether we killed it mid-spew or it finished on its own:
            # the capture is incomplete either way, so a diff would be a lie.
            return VerifyResult(
                status="error",
                note=f"output exceeded {_OUTPUT_LIMIT // 1024} KB limit",
                detail=[{
                    "stdin": stdin_text,
                    "expected": expected_stdout,
                    "stdout": stdout,
                    "stderr": stderr,
                }],
            )

        if proc.returncode != 0:
            # A crash / nonzero exit is an `error`, not a content `fail`.
            # stdout/stderr are already capped+marked by the readers.
            return VerifyResult(
                status="error",
                note=f"exited with code {proc.returncode}",
                detail=[{
                    "stdin": stdin_text,
                    "expected": expected_stdout,
                    "stdout": stdout,
                    "stderr": stderr,
                    "returncode": proc.returncode,
                }],
            )

        ok = _outputs_match(stdout, expected_stdout, problem_text)
        return VerifyResult(
            status="pass" if ok else "fail",
            note="output matched" if ok else "output differed",
            samples_total=1,
            samples_passed=1 if ok else 0,
            detail=[{
                "stdin": stdin_text,
                "expected": expected_stdout,
                "stdout": stdout,
                "stderr": stderr,
                "match": ok,
            }],
        )
    finally:
        # KILL_ON_JOB_CLOSE: closing the job handle terminates anything still
        # alive inside the job — the second kill mechanism after
        # kill_process_tree — and runs BEFORE rmtree so no straggler can hold
        # files in run_dir open. The teardown is asynchronous, so the delete
        # retries with backoff (B6).
        close_job(job_handle)
        _rmtree_with_retry(run_dir)


# --- pull sample I/O out of a pasted problem -----------------------------

# Matches the common LeetCode layout:
#     Input: nums = [2,7,11,15], target = 9
#     Output: [0,1]
# We capture everything after Input:/Output: up to the next label or a blank
# line. ``Explanation:`` (and the next ``Example``/``Constraints``) terminate the
# Output capture so we don't swallow prose. The leading char class also allows
# markdown emphasis wrappers (``*``/``_``/`` ` ``) before the label itself, e.g.
# ``**Input:**`` or `` `Input:` `` (A4) — the label's OWN closing wrapper (the
# ``**`` right after the colon) lands inside the captured value instead, which
# is why :func:`_strip_markdown_noise` is applied to every captured value below.
_INPUT_RE = re.compile(
    r"(?im)^[ \t>*_`-]*input\s*[:=]\s*(.*?)\s*$"
)
_OUTPUT_RE = re.compile(
    r"(?im)^[ \t>*_`-]*output\s*[:=]\s*(.*?)\s*$"
)
# Section markers that terminate the search for an ``Output:`` after an
# ``Input:``. A multi-line Input block (e.g. an array printed across several
# lines) can push Output well past a small fixed window, so instead of a fixed
# line count we scan forward until the Output — or bail at the next section so
# we never swallow prose or the following example's data.
_TERMINAL_RE = re.compile(
    r"(?im)^[ \t>*_`-]*(?:explanation|example|constraints?|follow[ -]?up|note)\b"
)

# Strips a leading/trailing run of markdown emphasis wrappers (bold ``**``,
# italic ``_``, inline code `` ` ``) and the whitespace they leave behind, e.g.
# from ``**Input:** value`` or `` `Output:` ``value``` `` (A4) — otherwise the
# label's own closing wrapper leaks into the synthesized stdin/expected and
# false-FAILs an otherwise-correct solution.
_MD_WRAP_RE = re.compile(r"^[\s`*_]+|[\s`*_]+$")

# Same idea, but for a line INSIDE a bare-label body (e.g. a 2-D grid printed
# across several lines under a bare ``Input:``): only markdown wrap characters
# are stripped, never LEADING whitespace, since a body line's indentation can
# be meaningful data (a nested list's own formatting) rather than incidental
# markdown padding (#11 — the old ``_MD_WRAP_RE`` stripped leading whitespace
# on every body line via its ``^[\s...]+`` half, silently de-indenting it).
_MD_WRAP_LEADING_NO_INDENT_RE = re.compile(r"^[`*_]+")
_MD_WRAP_TRAILING_RE = re.compile(r"[\s`*_]+$")


def _strip_markdown_noise(text: str) -> str:
    return _MD_WRAP_RE.sub("", text or "")


def _strip_markdown_noise_keep_indent(text: str) -> str:
    """Like :func:`_strip_markdown_noise` but preserves leading whitespace
    (#11): strips leading markdown wrap characters and trailing
    whitespace/markdown wrap characters only."""
    text = _MD_WRAP_LEADING_NO_INDENT_RE.sub("", text or "")
    return _MD_WRAP_TRAILING_RE.sub("", text)


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
        stdin_val = _strip_markdown_noise(m_in.group(1))
        # Scan forward for the matching Output:. No fixed window — a multi-line
        # Input block can push Output arbitrarily far down — but bail at a
        # section marker (Explanation/Example/Constraints/...) or another Input:,
        # so we never cross into prose or the next example.
        out_val = None
        j = i + 1
        while j < n:
            m_out = _OUTPUT_RE.match(lines[j])
            if m_out:
                out_val = _strip_markdown_noise(m_out.group(1))
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
        # ``Output:``. Take that body verbatim (indentation may be meaningful,
        # #11 — e.g. a 2-D grid's own row formatting), trimming only
        # surrounding blank lines and markdown wrap noise, never a line's own
        # leading whitespace.
        if not stdin_val:
            body = lines[i + 1:j]
            while body and not body[0].strip():
                body.pop(0)
            while body and not body[-1].strip():
                body.pop()
            stdin_val = "\n".join(_strip_markdown_noise_keep_indent(ln) for ln in body)

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
                out_body.append(_strip_markdown_noise(line.strip()))
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
            return _verify_python_samples(
                code, samples, timeout=config.verify_timeout(), problem_text=problem_text
            )

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


def _verify_python_samples(
    code: str, samples: list, *, timeout: float = 10.0, problem_text: str = ""
) -> VerifyResult:
    """Run ``code`` against each parsed sample and aggregate the verdict.

    Aggregation keeps three verdicts — ``pass`` (all matched), ``error`` (every
    non-pass sample crashed), ``fail`` (at least one wrong-answer). A mixed run
    that both fails and errors reports ``fail`` but the note names the errored
    count too (``"X/Y passed, Z errored"``), so a crash is never silently folded
    into a plain wrong-answer verdict. ``timeout`` is passed straight through as
    a float (B5) — no truncating ``int()`` cast, so a sub-second budget like
    ``0.5`` is honoured instead of becoming ``0``.
    """
    total = len(samples)
    passed = 0
    errored = 0
    detail: list = []
    for idx, s in enumerate(samples, start=1):
        r = verify_python(
            code, s.stdin, s.expected_stdout, timeout=timeout, problem_text=problem_text
        )
        # B4: seed the entry with the sample's own stdin/expected AND the
        # verifier's note (the timeout/crash reason) up front — some error
        # paths (timeout, "could not run", ...) carry a `note` but no
        # `detail`, and the note was previously dropped entirely, leaving the
        # user with "errored 1/1" and no explanation. `r.detail`, when
        # present, still overrides with the actual captured stdout/stderr.
        entry = {
            "sample": idx,
            "status": r.status,
            "note": r.note,
            "stdin": s.stdin,
            "expected": s.expected_stdout,
        }
        if r.detail:
            entry.update(r.detail[0])
        detail.append(entry)
        if r.status == "pass":
            passed += 1
        elif r.status == "error":
            errored += 1

    if passed == total:
        status, note = "pass", f"all {total} sample(s) passed"
    elif errored and passed == 0 and errored == total - passed:
        # every non-passing sample crashed — a pure error, not a wrong answer
        status, note = "error", f"code errored on {errored}/{total} sample(s)"
    elif errored:
        # mixed: some wrong answers AND some crashes — surface both counts
        status, note = "fail", f"{passed}/{total} sample(s) passed, {errored} errored"
    else:
        status, note = "fail", f"{passed}/{total} sample(s) passed"

    return VerifyResult(
        status=status,
        note=note,
        samples_total=total,
        samples_passed=passed,
        detail=detail,
    )
