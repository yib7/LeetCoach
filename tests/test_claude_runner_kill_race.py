"""SP2 fix I2: a killer can never act on a closed job handle or a reaped pid.

``ClaudeRun.cancel()`` and the wall-clock watchdog both run the tree-kill on
another thread, outside any lock the runner's teardown took. A killer still
blocked in ``taskkill`` could therefore resume AFTER the runner had closed its
Job Object handle (``TerminateJobObject`` on a closed - possibly recycled -
handle), and on POSIX ``killpg`` a pid the runner had already reaped.

The job API is replaced by a recorder, and the pid-based tree-kill by a
stand-in that kills the child at once but then "stays blocked" (like a slow
``taskkill``) - so the teardown races it deterministically. No real `claude`
is spawned: this Python interpreter stands in for it.
"""
from __future__ import annotations

import sys
import threading
import time

import pytest

import claude_cli
import proc_util


class _JobRecorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.events: list[str] = []
        self.violations: list[str] = []
        self.closed = False

    def create(self):
        return self  # the "handle"

    def assign(self, job, proc):
        return True

    def close(self, job):
        with self.lock:
            self.events.append("close")
            self.closed = True

    def terminate(self, job):
        with self.lock:
            self.events.append("terminate")
            if self.closed:
                self.violations.append("terminate on a closed job")
        return True


@pytest.fixture
def jobs(monkeypatch):
    rec = _JobRecorder()
    monkeypatch.setattr(proc_util, "create_kill_on_close_job", rec.create)
    monkeypatch.setattr(proc_util, "assign_to_job", rec.assign)
    monkeypatch.setattr(proc_util, "close_job", rec.close)
    monkeypatch.setattr(proc_util, "terminate_job", rec.terminate)
    return rec


@pytest.fixture
def slow_tree_kill(monkeypatch):
    """The pid-based tree-kill kills the child at once, then blocks 0.6 s."""
    calls: list = []

    def fake(proc, *, group=False):
        calls.append(proc.returncode)  # None = the pid was not yet reaped
        try:
            proc.kill()
        except OSError:
            pass
        time.sleep(0.6)
        return True

    monkeypatch.setattr(claude_cli, "_kill_process_tree", fake)
    return calls


_SLEEPER = (
    "import sys, time\n"
    "sys.stdin.read()\n"
    "sys.stdout.write('first\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(40)\n"
)


def test_cancel_racing_teardown_never_terminates_a_closed_job(jobs, slow_tree_kill):
    handle = claude_cli.ClaudeRun()
    gen = claude_cli._real_runner([sys.executable, "-c", _SLEEPER], "ping", handle=handle)
    assert next(gen).strip() == "first"
    canceller = threading.Thread(target=handle.cancel, daemon=True)
    canceller.start()
    with pytest.raises(claude_cli.ClaudeCancelledError):
        next(gen)
    canceller.join(10)
    assert not canceller.is_alive()
    assert jobs.violations == []
    assert jobs.events[-1] == "close"  # the in-flight killer finished first
    assert all(rc is None for rc in slow_tree_kill), "pid-kill ran after the reap"


def test_watchdog_racing_teardown_never_terminates_a_closed_job(
    jobs, slow_tree_kill, monkeypatch
):
    monkeypatch.setenv("LEETCOACH_RUN_TIMEOUT", "1")
    script = "import sys, time\nsys.stdin.read()\ntime.sleep(40)\n"
    gen = claude_cli._real_runner([sys.executable, "-c", script], "ping")
    with pytest.raises(claude_cli.ClaudeUnavailableError, match="timed out"):
        list(gen)
    # the teardown returned only after the watchdog's kill was done
    assert jobs.violations == []
    assert "terminate" in jobs.events and jobs.events[-1] == "close"
    assert all(rc is None for rc in slow_tree_kill)


def test_a_killer_called_after_teardown_is_a_no_op(jobs, slow_tree_kill):
    handle = claude_cli.ClaudeRun()
    script = "import sys\nsys.stdin.read()\nsys.stdout.write('only\\n')\n"
    gen = claude_cli._real_runner([sys.executable, "-c", script], "ping", handle=handle)
    assert next(gen).strip() == "only"
    killer = handle._killer  # e.g. a cancel() that grabbed it just before detach
    assert killer is not None
    assert list(gen) == []
    assert handle._killer is None
    killer()  # late: the job is closed and the pid reaped
    assert jobs.violations == []
    assert "terminate" not in jobs.events
    assert slow_tree_kill == [], "a late killer touched a reaped pid"
