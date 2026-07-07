"""Keystone wrapper around the `claude` CLI (`claude -p`, no API key).

The whole app drives Claude through this one module. Design goals:

1. The prompt (which can be a huge pasted LeetCode problem) is fed on **stdin**,
   never as an argv argument, so it never hits OS arg-length or shell-escaping
   limits.
2. Output is parsed from ``--output-format stream-json`` so callers get
   incremental **text deltas** suitable for streaming to a browser over SSE.
3. The subprocess is **injectable** (`runner=`) so tests can substitute a fake
   that yields canned stream-json lines without spawning `claude`.
4. Availability is checkable up front (`is_available()`) and a clear error is
   raised if the binary is missing.

Observed stream-json line shapes (live, `claude` v2.1.x). Lines are
newline-delimited JSON objects; we only care about a couple of them:

  * true streaming text (with `--include-partial-messages`)::

        {"type":"stream_event","event":{"type":"content_block_delta",
         "index":1,"delta":{"type":"text_delta","text":"Hel"}}}

    Thinking blocks arrive on the same `content_block_delta` channel but as
    ``signature_delta`` / ``thinking`` deltas — those are NOT answer text and
    are skipped.

  * fallback complete-block shape (no partial messages)::

        {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}

  * a final ``{"type":"result","subtype":"success","result":"..."}`` echoes the
    full answer; we ignore it for deltas so text is never double-counted.

`stream-json` output on this CLI *requires* ``--verbose``; the wrapper always
passes it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Iterable, Iterator, Optional

import config


class ClaudeUnavailableError(RuntimeError):
    """Raised when the `claude` binary cannot be found / run."""


# --- availability --------------------------------------------------------

def is_available(*, which: Callable[[str], Optional[str]] = shutil.which) -> bool:
    """Return True if the configured `claude` binary is resolvable on PATH.

    `which` is injectable purely so tests can exercise both branches without
    depending on what is installed on the machine.
    """
    return which(config.claude_bin()) is not None


# --- subprocess runner (the only real-IO part) ---------------------------

def _kill_process_tree(proc: "subprocess.Popen[str]") -> bool:
    """Best-effort kill of `proc` AND its descendants. Returns True if a
    tree-kill mechanism was invoked (not necessarily that it succeeded).

    On Windows the installed `claude` binary is typically an npm shim
    (``claude.cmd`` launching node via a child process), so `proc.terminate()`
    only kills the shim — the actual node process doing the work is left
    running. ``taskkill /T`` walks the process tree by PID and kills the
    whole thing. No new dependency (psutil) is pulled in for this; taskkill
    ships with Windows. Falls back to terminate()/kill() on non-Windows or if
    taskkill itself fails to launch.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
            return True
        except OSError:
            pass  # taskkill missing/unusable — fall through to terminate()
    proc.terminate()
    return False


def _real_runner(argv: list[str], stdin_text: str) -> Iterator[str]:
    """Spawn `claude`, feed `stdin_text`, and yield stdout lines as they arrive.

    The prompt is written to the child's stdin and the pipe is closed, so the
    child sees EOF and starts producing output, which we read line-by-line for
    incremental streaming.
    """
    # stderr goes to a temp file, not a PIPE: an unread stderr PIPE can fill its
    # ~64KB OS buffer and deadlock the child (it blocks writing stderr while we
    # block reading stdout). A file has no such limit; we read it back only if
    # the child exits nonzero. Binary file -> decode manually (text= applies to
    # the stdin/stdout pipes, not a redirected file handle).
    stderr_file = tempfile.TemporaryFile()
    popen_kwargs: dict = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": stderr_file,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,  # line-buffered so deltas surface promptly
    }
    if os.name == "nt":
        # Don't pop a console window when launched from a GUI/Flask process.
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # On Windows the `claude` entry point is a .CMD/.EXE shim; bare-name Popen
    # does not apply PATHEXT, so resolve argv[0] to the full path that
    # shutil.which found (which DOES honour PATHEXT). No-op on POSIX / when
    # already absolute.
    resolved = shutil.which(argv[0])
    if resolved:
        argv = [resolved, *argv[1:]]

    proc = subprocess.Popen(argv, **popen_kwargs)
    drained = False
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(stdin_text)
        proc.stdin.close()
        for line in proc.stdout:
            yield line
        drained = True  # stdout hit EOF naturally — the child is done
    finally:
        # If we did NOT drain stdout to EOF, the generator is being closed early
        # — the SSE client disconnected (Flask throws GeneratorExit into us at
        # the next yield) or an exception unwound the consumer. The `claude`
        # subprocess would otherwise keep running to completion and keep burning
        # subscription usage, so terminate it. Without this, proc.wait() below
        # would also block until the abandoned child finishes.
        if not drained and proc.poll() is None:
            _kill_process_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()  # last resort if terminate/taskkill was ignored
        if proc.stdout is not None:
            proc.stdout.close()
        returncode = proc.wait()
        try:
            # Only surface a nonzero-exit error on a normal, fully-drained run.
            # A process we terminated on client disconnect exits nonzero by
            # design; that's a cancellation, not a Claude failure to report.
            if drained and returncode != 0:
                stderr_file.seek(0)
                stderr = stderr_file.read().decode("utf-8", "replace")
                raise ClaudeUnavailableError(
                    f"`{config.claude_bin()}` exited with code {returncode}. "
                    f"Is the claude CLI installed and authenticated?\n{stderr.strip()}"
                )
        finally:
            stderr_file.close()


# --- stream-json parsing -------------------------------------------------

def _iter_text_deltas(lines: Iterable[str]) -> Iterator[str]:
    """Parse newline-delimited stream-json `lines` into visible text deltas.

    Strategy (robust to both partial-message and complete-block modes):

    * Prefer ``stream_event`` ``text_delta`` chunks — true incremental output.
    * If the whole stream contained no such events, fall back to emitting the
      text content blocks from ``assistant`` messages (complete-block mode).
    * Ignore everything else (system/init lines, thinking/signature deltas,
      the final ``result`` echo, blank lines, and any non-JSON noise).
    """
    saw_stream_event_text = False
    assistant_fallback: list[str] = []

    # `lines` is typically the generator returned by `_real_runner`. When the
    # SSE client disconnects, Flask closes the OUTERMOST generator (the one
    # `run()` returns, which is this generator via `yield from`); CPython
    # propagates that close() down through the `yield from` chain into this
    # `for` loop as a GeneratorExit, which in turn reaches `lines` only
    # because `for` calls `lines.close()` implicitly on GC — relying on
    # refcounting timing, not a guaranteed protocol. Close `lines` explicitly
    # so `_real_runner`'s cleanup (which terminates the `claude` subprocess)
    # runs deterministically regardless of GC timing.
    try:
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Defensive: a stray non-JSON line must never crash the stream.
                continue
            if not isinstance(obj, dict):
                continue

            kind = obj.get("type")

            if kind == "stream_event":
                event = obj.get("event") or {}
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            saw_stream_event_text = True
                            yield text
                # thinking/signature deltas and other event types: ignored
                continue

            if kind == "assistant":
                # Record assistant text in case no stream_event text ever appears.
                message = obj.get("message") or {}
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        assistant_fallback.append(block.get("text", ""))
                continue

            # system / result / rate_limit_event / anything else: not delta text.
    finally:
        close = getattr(lines, "close", None)
        if callable(close):
            close()

    if not saw_stream_event_text and assistant_fallback:
        joined = "".join(assistant_fallback)
        if joined:
            yield joined


# --- public entry point --------------------------------------------------

def run(
    prompt: str,
    *,
    model: Optional[str] = None,
    runner: Optional[Callable[[list[str], str], Iterable[str]]] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Iterator[str]:
    """Stream Claude's answer to `prompt` as a sequence of text deltas.

    Parameters
    ----------
    prompt:
        The full prompt. Delivered to the child process via **stdin**, so it can
        be arbitrarily large.
    model:
        Model id for ``--model``. Defaults to ``config.model()``.
    runner:
        Injectable subprocess runner ``runner(argv, stdin_text) -> Iterable[str]``
        yielding raw stdout lines. Defaults to the real subprocess runner. Tests
        pass a fake so no real `claude` is spawned.
    which:
        Injectable PATH resolver, only consulted for the availability guard when
        using the real runner.

    Yields
    ------
    str
        Visible answer text, delta by delta (assemble by concatenation).
    """
    if model is None:
        model = config.model()

    use_real = runner is None
    if use_real:
        # Only guard availability for the real path; fakes don't need a binary.
        if not is_available(which=which):
            raise ClaudeUnavailableError(
                f"The `{config.claude_bin()}` CLI was not found on PATH. Install "
                "Claude Code and ensure `claude` is runnable, or set "
                "LEETCOACH_CLAUDE_BIN to its full path."
            )
        runner = _real_runner

    argv = [
        config.claude_bin(),
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",  # gives true incremental text_delta chunks
        "--verbose",                   # required by the CLI for stream-json
        "--model",
        model,
    ]

    lines = runner(argv, prompt)
    yield from _iter_text_deltas(lines)
