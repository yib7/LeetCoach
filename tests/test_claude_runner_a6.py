"""A6 / C4 / C5: the real `claude` runner can never hang, and cancels cleanly.

* The wall-clock watchdog is keyed on ``reading_done`` only: a `claude` that
  exits while a grandchild still holds its stdout pipe used to make the read
  loop block until the grandchild died (the old watchdog bailed out as soon as
  the direct child had exited).
* Reading stops at the terminal ``result`` event; a straggler (the CLI itself
  lingering, or a pipe-holding grandchild) cannot keep the run open.
* The whole `claude` tree lives in a kill-on-close Job Object (Windows) / its
  own process group (POSIX, C5), so a kill reaches grandchildren whose parent
  already exited.
* ``ClaudeRun.cancel()`` from another thread kills the process and the
  iterator raises :class:`claude_cli.ClaudeCancelledError`.
* C4: a consumer disconnect while the (large) prompt is still being written to
  stdin must not raise a spurious ``ClaudeUnavailableError`` out of ``close()``.

No real `claude` is spawned: this Python interpreter stands in for it.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time

import pytest
from _helpers import pid_alive, wait_dead

import claude_cli


def _kill_pid(pid):
    if pid and pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _drive(argv, stdin_text, *, join=20.0, handle=None):
    """Run ``_real_runner`` to completion on a worker thread; returns
    ``(lines, exc, elapsed, finished)``."""
    out: dict = {"lines": []}

    def go():
        try:
            for line in claude_cli._real_runner(argv, stdin_text, handle=handle):
                out["lines"].append(line)
        except BaseException as exc:  # noqa: BLE001 - inspected by the caller
            out["exc"] = exc

    start = time.monotonic()
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(join)
    return out["lines"], out.get("exc"), time.monotonic() - start, not t.is_alive()


def _grandchild_pid(lines):
    for line in lines:
        if line.startswith("GRANDCHILD "):
            return int(line.split()[1])
    return None


# The stand-in `claude`: spawns a grandchild that inherits (and holds) stdout,
# reports its pid, then the direct child exits at once.
_PIPE_HOLDER = (
    "import subprocess, sys\n"
    "sys.stdin.read()\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'],\n"
    "                     stdin=subprocess.DEVNULL, stdout=sys.stdout,\n"
    "                     stderr=subprocess.DEVNULL)\n"
    "sys.stdout.write('GRANDCHILD %d\\n' % g.pid)\n"
    "sys.stdout.flush()\n"
    "__EXTRA__"
)


def test_watchdog_fires_when_child_exits_but_grandchild_holds_stdout(monkeypatch):
    monkeypatch.setenv("LEETCOACH_RUN_TIMEOUT", "2")
    script = _PIPE_HOLDER.replace("__EXTRA__", "")
    lines, exc, elapsed, finished = _drive([sys.executable, "-c", script], "ping")
    gpid = _grandchild_pid(lines)
    try:
        assert finished, "runner hung on a pipe-holding grandchild (watchdog never fired)"
        assert isinstance(exc, claude_cli.ClaudeUnavailableError)
        assert "timed out" in str(exc).lower()
        assert elapsed < 2 + 8, f"took {elapsed:.1f}s for a 2s timeout"
        # the kill reached the grandchild even though its parent had exited
        assert gpid is not None and wait_dead(gpid), "pipe-holding grandchild survived"
    finally:
        _kill_pid(gpid)


def test_reading_stops_at_result_even_if_grandchild_holds_stdout(monkeypatch):
    monkeypatch.setenv("LEETCOACH_RUN_TIMEOUT", "60")
    result = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                         "result": "done"})
    script = _PIPE_HOLDER.replace(
        "__EXTRA__", f"sys.stdout.write({result!r} + '\\n')\nsys.stdout.flush()\n"
    )
    lines, exc, elapsed, finished = _drive([sys.executable, "-c", script], "ping")
    gpid = _grandchild_pid(lines)
    try:
        assert finished, "runner kept reading past the terminal result event"
        assert exc is None, f"unexpected error: {exc!r}"
        assert elapsed < 15
        assert json.loads(lines[-1])["type"] == "result"
    finally:
        _kill_pid(gpid)


def test_lingering_cli_after_result_is_killed_not_awaited(monkeypatch):
    monkeypatch.setenv("LEETCOACH_RUN_TIMEOUT", "60")
    monkeypatch.setattr(claude_cli, "RESULT_EXIT_GRACE", 0.5)
    result = json.dumps({"type": "result", "subtype": "success", "result": "done"})
    script = (
        "import sys, time\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({result!r} + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(40)\n"
    )
    lines, exc, elapsed, finished = _drive([sys.executable, "-c", script], "ping")
    assert finished
    assert exc is None, f"a CLI we stopped after its result is not a failure: {exc!r}"
    assert elapsed < 12
    assert len(lines) == 1


def test_cancel_from_another_thread_kills_and_raises_cancelled():
    script = (
        "import sys, time\n"
        "sys.stdin.read()\n"
        "sys.stdout.write('first\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(40)\n"
    )
    handle = claude_cli.ClaudeRun()
    gen = claude_cli._real_runner([sys.executable, "-c", script], "ping", handle=handle)
    assert next(gen).strip() == "first"
    threading.Timer(0.3, handle.cancel).start()
    start = time.monotonic()
    with pytest.raises(claude_cli.ClaudeCancelledError):
        next(gen)
    assert time.monotonic() - start < 10
    assert handle.cancelled


def test_cancel_via_run_handle_full_chain():
    script = (
        "import sys, time\n"
        "sys.stdin.read()\n"
        "print('{\"type\": \"stream_event\", \"event\": {\"type\": \"content_block_delta\", "
        "\"delta\": {\"type\": \"text_delta\", \"text\": \"hi\"}}}')\n"
        "sys.stdout.flush()\n"
        "time.sleep(40)\n"
    )

    def runner(argv, stdin_text, **kwargs):
        return claude_cli._real_runner([sys.executable, "-c", script], stdin_text, **kwargs)

    run = claude_cli.run("x", runner=runner)
    assert next(run) == "hi"
    threading.Timer(0.3, run.cancel).start()
    with pytest.raises(claude_cli.ClaudeCancelledError):
        next(run)


def test_cancel_before_spawn_kills_immediately():
    script = "import sys, time\nsys.stdin.read()\ntime.sleep(40)\n"
    handle = claude_cli.ClaudeRun()
    handle.cancel()  # raced ahead of the spawn
    lines, exc, elapsed, finished = _drive(
        [sys.executable, "-c", script], "ping", handle=handle
    )
    assert finished and elapsed < 10
    assert isinstance(exc, claude_cli.ClaudeCancelledError)


def test_disconnect_during_stdin_write_does_not_raise():
    """C4: the child never reads stdin, so the 4 MB prompt write is still
    blocked when the consumer closes the generator. The kill then breaks the
    pipe mid-write; that must be treated as the cancellation it is."""
    script = (
        "import sys, time\n"
        "sys.stdout.write('first\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(40)\n"
    )
    gen = claude_cli._real_runner([sys.executable, "-c", script], "x" * (4 * 1024 * 1024))
    assert next(gen).strip() == "first"
    start = time.monotonic()
    gen.close()  # must not raise ClaudeUnavailableError
    assert time.monotonic() - start < 15
