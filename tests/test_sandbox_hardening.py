"""SP3 sandbox hardening: the trusted bootstrap handshake (A5), the audit-hook
defence-in-depth (C6), raw-byte output capture + temp-dir hygiene (B6).

Like ``test_sandbox.py`` these run REAL local Python subprocesses (no Claude
anywhere): the sandbox's job is to run untrusted Python, so exercising it for
real is the honest test.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

import sandbox

# --- A5: bootstrap handshake ------------------------------------------------


def _spy_popen(monkeypatch):
    """Record every argv the sandbox hands to Popen (pass-through)."""
    calls: list = []
    real_popen = subprocess.Popen

    class _Spy(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **k):
            calls.append(list(args) if isinstance(args, (list, tuple)) else args)
            super().__init__(args, *a, **k)

    monkeypatch.setattr(sandbox.subprocess, "Popen", _Spy)
    return calls


def test_child_is_the_base_interpreter_in_isolated_mode_running_the_bootstrap(monkeypatch):
    """A5: never the venv launcher (``sys.executable`` in a venv on Windows is
    a stub whose REAL interpreter is a grandchild that can start outside the
    job). The child is ``sys._base_executable`` (fallback ``sys.executable``)
    with ``-I``, running the trusted bootstrap, which then runs the solution."""
    calls = _spy_popen(monkeypatch)
    r = sandbox.verify_python("print(42)\n", "", "42")
    assert r.status == "pass", r
    argv = calls[0]
    expected_exe = getattr(sys, "_base_executable", None) or sys.executable
    if not os.path.isfile(expected_exe):
        expected_exe = sys.executable
    assert os.path.normcase(argv[0]) == os.path.normcase(expected_exe)
    assert "-I" in argv
    assert os.path.normcase(sandbox._BOOTSTRAP_PATH) in [os.path.normcase(a) for a in argv]
    assert any(a.endswith("solution.py") for a in argv)


def test_child_python_falls_back_to_sys_executable(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "_base_executable", r"Z:\no\such\python.exe", raising=False)
    assert sandbox._child_python() == sys.executable
    monkeypatch.delattr(sandbox.sys, "_base_executable", raising=False)
    assert sandbox._child_python() == sys.executable


def test_untrusted_code_cannot_run_before_the_job_is_assigned(monkeypatch):
    """A5 ordering: the solution's first statement must not execute until the
    parent has assigned the job object and released the go byte. The spy
    stalls the assignment for 1.5 s (longer than interpreter startup — a
    GIL-starved parent does exactly this) and then checks that the solution's
    first-line marker file does not exist yet."""
    seen: dict = {}
    real_assign = sandbox.assign_to_job

    def slow_assign(job, proc):
        time.sleep(1.5)
        run_dir = seen["run_dir"]
        seen["started_before_assign"] = os.path.exists(os.path.join(run_dir, "started"))
        return real_assign(job, proc)

    real_popen = subprocess.Popen

    class _Spy(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **k):
            if "run_dir" not in seen and k.get("cwd"):
                seen["run_dir"] = k["cwd"]
            super().__init__(args, *a, **k)

    monkeypatch.setattr(sandbox.subprocess, "Popen", _Spy)
    monkeypatch.setattr(sandbox, "assign_to_job", slow_assign)
    code = "open('started', 'w').write('x')\nprint('ok')\n"
    r = sandbox.verify_python(code, "", "ok", timeout=15)
    assert r.status == "pass", r
    assert seen["started_before_assign"] is False, (
        "solution code ran before the job object was assigned"
    )


@pytest.mark.parametrize(
    "reader",
    [
        "import sys\ndata = sys.stdin.read()",
        "import sys\ndata = sys.stdin.readline() + sys.stdin.readline()",
        "data = input() + '\\n' + input() + '\\n'",
        "data = open(0).read()",
        "import sys\ndata = sys.stdin.buffer.read().decode()",
        "import os\ndata = os.read(0, 4096).decode()",
    ],
)
def test_solution_sees_exactly_the_sample_stdin(reader):
    """The go byte is consumed by the bootstrap with an unbuffered 1-byte
    read, so EVERY way a solution reads stdin (text layer, buffer, raw fd 0,
    ``open(0)``) sees the sample input and nothing else."""
    code = reader + "\nprint(repr(data))\n"
    r = sandbox.verify_python(code, "nums = [2,7]\ntarget = 9\n", repr("nums = [2,7]\ntarget = 9\n"))
    assert r.status == "pass", r


def test_solution_runs_as_main_with_its_own_argv_and_utf8_io():
    code = (
        "import sys, os\n"
        "s = sys.stdin.readline().strip()\n"
        "print(__name__, os.path.basename(sys.argv[0]), len(sys.argv), s[::-1])\n"
    )
    r = sandbox.verify_python(code, "héllo ✓\n", "__main__ solution.py 1 ✓ olléh")
    assert r.status == "pass", r


def test_solution_exit_code_and_traceback_are_preserved():
    r = sandbox.verify_python("import sys\nsys.exit(3)\n", "", "x")
    assert r.status == "error" and "code 3" in r.note, r
    r = sandbox.verify_python("raise ValueError('boom')\n", "", "x")
    assert r.status == "error", r
    assert "ValueError: boom" in r.detail[0]["stderr"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object caps")
def test_memory_cap_holds_under_gil_contention():
    """A5 regression (in-suite slice of the SP3 checkpoint stress): with two
    busy Python threads in the parent delaying the post-spawn job assignment,
    a 700 MB allocation must still be killed by the 512 MB cap every time.
    Before the bootstrap handshake it escaped in ~19/20 runs."""
    stop = threading.Event()

    def busy():
        while not stop.is_set():
            pass

    threads = [threading.Thread(target=busy, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        hog = "data = bytearray(700 * 1024 * 1024)\nprint('ALLOCATED')\n"
        for _ in range(3):
            r = sandbox.verify_python(hog, "", "ALLOCATED", timeout=20)
            assert r.status == "error", r
            assert "MemoryError" in r.detail[0]["stderr"], r
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)
