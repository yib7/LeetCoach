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


# --- C6: audit-hook defence in depth ----------------------------------------
#
# Every probe wraps ONE action and prints BLOCKED only when the sandbox's own
# PermissionError fired (its message names the sandbox) — directly, or as the
# cause/context of a wrapper (urllib's URLError, ctypes' import-time
# AttributeError) — so an unrelated failure (connection refused, file
# missing) can never pass as "blocked".

_PROBE = (
    "import sys\n"
    "arg = sys.stdin.readline().strip()\n"
    "def _sandboxed(e):\n"
    "    while e is not None:\n"
    "        if 'LeetCoach sandbox' in str(e) or 'LeetCoach sandbox' in str(getattr(e, 'reason', '')):\n"
    "            return True\n"
    "        e = e.__cause__ or e.__context__\n"
    "    return False\n"
    "try:\n"
    "{body}"
    "    print('ALLOWED')\n"
    "except Exception as e:\n"
    "    print('BLOCKED' if _sandboxed(e) else 'OTHER ' + type(e).__name__ + ' ' + str(e))\n"
)


def _probe(body: str) -> str:
    lines = "".join("    " + ln + "\n" for ln in body.strip("\n").split("\n"))
    return _PROBE.format(body=lines)


def _run_probe(body: str, arg: str = "", **kw) -> sandbox.VerifyResult:
    return sandbox.verify_python(_probe(body), arg + "\n", "BLOCKED", timeout=15, **kw)


NORMAL_SOLUTION = """\
import bisect, collections, heapq, itertools, json, math, re, string, sys
import functools, threading
from functools import lru_cache, cache
from typing import List, Optional

sys.setrecursionlimit(10 ** 6)

@lru_cache(maxsize=None)
def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)

@cache
def depth(n):
    return 0 if n == 0 else 1 + depth(n - 1)

def main():
    nums = list(map(int, sys.stdin.readline().split()))
    h = list(nums)
    heapq.heapify(h)
    c = collections.Counter(nums)
    dq = collections.deque(sorted(nums))
    i = bisect.bisect_left(dq, 3)
    with open('scratch.txt', 'w') as f:
        f.write(json.dumps(nums))
    with open('scratch.txt') as f:
        back = json.loads(f.read())
    import tempfile
    with tempfile.TemporaryFile() as tf:
        tf.write(b'x')
    out = [fib(30), depth(3000), heapq.heappop(h), c.most_common(1)[0][0], i,
           len(back), math.comb(5, 2), bool(re.match(r'\\d', '7')),
           len(list(itertools.permutations(range(4))))]
    print(out)

threading.stack_size(64 * 1024 * 1024)
t = threading.Thread(target=main)
t.start()
t.join()
"""


def test_normal_leetcode_solution_is_unaffected_by_the_audit_hook():
    """C6 must never break an ordinary solution: stdlib data structures,
    lru_cache/cache, a raised recursion limit with a big thread stack, stdin,
    print, and scratch files INSIDE the run dir (incl. tempfile, whose TEMP
    points there)."""
    r = sandbox.verify_python(
        NORMAL_SOLUTION, "5 3 1 3\n", "[832040, 3000, 1, 3, 1, 4, 10, True, 24]"
    )
    assert r.status == "pass", r


def test_write_outside_the_run_dir_is_blocked(tmp_path):
    target = tmp_path / "escape.txt"
    r = _run_probe("open(arg, 'w').write('pwned')", str(target))
    assert r.status == "pass", r
    assert not target.exists()


@pytest.mark.parametrize(
    "body",
    [
        "open(arg, 'a').write('pwned')",
        "open(arg, 'r+')",
        "open(arg + '.new', 'xb')",
        "import os\nos.open(arg, os.O_WRONLY | os.O_CREAT)",
        "import pathlib\npathlib.Path(arg).write_text('pwned')",
        "import os\nopen(os.path.join('..', os.path.basename(arg)), 'w')",
    ],
)
def test_every_write_flavour_outside_the_run_dir_is_blocked(tmp_path, body):
    target = tmp_path / "victim.txt"
    target.write_text("original", encoding="utf-8")
    r = _run_probe(body, str(target))
    assert r.status == "pass", r
    assert target.read_text(encoding="utf-8") == "original"
    assert not (tmp_path / "victim.txt.new").exists()


@pytest.mark.parametrize(
    "body",
    [
        "import os\nos.remove(arg)",
        "import os\nos.rename(arg, arg + '.moved')",
        "import shutil\nshutil.rmtree(__import__('os').path.dirname(arg))",
    ],
)
def test_deleting_or_moving_files_outside_the_run_dir_is_blocked(tmp_path, body):
    victim_dir = tmp_path / "keep"
    victim_dir.mkdir()
    target = victim_dir / "victim.txt"
    target.write_text("original", encoding="utf-8")
    r = _run_probe(body, str(target))
    assert r.status == "pass", r
    assert target.read_text(encoding="utf-8") == "original"


def test_reading_a_non_secret_file_outside_the_run_dir_is_allowed(tmp_path):
    """Reads are NOT blanket-blocked (imports need them) — only secret paths."""
    plain = tmp_path / "plain.txt"
    plain.write_text("hello", encoding="utf-8")
    code = "import sys\nprint(open(sys.stdin.readline().strip()).read())\n"
    r = sandbox.verify_python(code, str(plain) + "\n", "hello")
    assert r.status == "pass", r


@pytest.fixture
def fake_secrets(tmp_path, monkeypatch):
    """A dummy 'secret' dir + file registered as secret paths — never the real
    ~/.claude or .env."""
    secret_dir = tmp_path / "fake-dot-claude"
    secret_dir.mkdir()
    (secret_dir / ".credentials.json").write_text('{"dummy": true}', encoding="utf-8")
    secret_file = tmp_path / "fake.env"
    secret_file.write_text("DUMMY=1\n", encoding="utf-8")
    monkeypatch.setattr(sandbox, "_secret_paths", lambda: [str(secret_dir), str(secret_file)])
    return secret_dir, secret_file


@pytest.mark.parametrize(
    "body",
    [
        "open(arg + '/.credentials.json').read()",
        "open(arg + '/.credentials.json', 'rb').read()",
        "import os\nos.open(arg + '/.credentials.json', os.O_RDONLY)",
        "import os\nos.listdir(arg)",
        "import os\nlist(os.scandir(arg))",
        "import pathlib\npathlib.Path(arg, '.credentials.json').read_text()",
    ],
)
def test_reading_a_registered_secret_dir_is_blocked(fake_secrets, body):
    secret_dir, _ = fake_secrets
    r = _run_probe(body, str(secret_dir))
    assert r.status == "pass", r


def test_reading_a_registered_secret_file_is_blocked(fake_secrets):
    _, secret_file = fake_secrets
    r = _run_probe("open(arg).read()", str(secret_file))
    assert r.status == "pass", r


def test_default_secret_paths_cover_claude_credentials_and_the_repo_env():
    """The real list, checked as strings only (nothing is opened)."""
    paths = {os.path.normcase(os.path.abspath(p)) for p in sandbox._secret_paths()}
    home = os.path.expanduser("~")
    repo = os.path.dirname(os.path.abspath(sandbox.__file__))
    for must in (
        os.path.join(home, ".claude"),
        os.path.join(home, ".claude.json"),
        os.path.join(home, ".ssh"),
        os.path.join(repo, ".env"),
    ):
        assert os.path.normcase(os.path.abspath(must)) in paths, must


@pytest.mark.parametrize(
    "body",
    [
        "import socket\ns = socket.socket()\ns.settimeout(2)\ns.connect(('127.0.0.1', 9))",
        "import socket\nsocket.create_connection(('127.0.0.1', 9), timeout=2)",
        "import socket\ns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "s.sendto(b'x', ('127.0.0.1', 9))",
        "import urllib.request\nurllib.request.urlopen('http://127.0.0.1:9/', timeout=2)",
    ],
)
def test_network_is_blocked(body):
    r = _run_probe(body)
    assert r.status == "pass", r


@pytest.mark.parametrize(
    "body",
    [
        "import subprocess\nsubprocess.run([sys.executable, '-c', 'pass'])",
        "import os\nos.system('echo hi')",
        "import os\nos.execv(sys.executable, [sys.executable, '-c', 'pass'])",
        "import os\nos.spawnv(os.P_WAIT, sys.executable, [sys.executable, '-c', 'pass'])",
        "import os\nos.popen('echo hi').read()",
        "import multiprocessing as mp\np = mp.Process(target=print)\np.start()\np.join()",
    ],
)
def test_process_creation_is_blocked(body):
    r = _run_probe(body)
    assert r.status == "pass", r


def test_ctypes_library_loading_is_blocked():
    body = (
        "import ctypes\n"
        "lib = ctypes.WinDLL('kernel32') if sys.platform == 'win32' else ctypes.CDLL(None)\n"
        "lib.GetTickCount if sys.platform == 'win32' else lib.getpid"
    )
    r = _run_probe(body)
    assert r.status == "pass", r


def test_symlink_creation_is_blocked(tmp_path):
    r = _run_probe("import os\nos.symlink(arg, 'link')", str(tmp_path))
    assert r.status == "pass", r


def test_audit_hook_can_be_disabled_for_containment_layer_tests():
    """The other layers (job caps, tree kill, capture of a grandchild's pipe)
    are tested by spawning a grandchild on purpose, so the hook has an off
    switch that production code never uses."""
    code = "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'print(7)'])\n"
    r = sandbox.verify_python(code, "", "7", audit_hook=False)
    assert r.status == "pass", r
    r = sandbox.verify_python(code, "", "7")
    assert r.status == "error", r
    assert "LeetCoach sandbox" in r.detail[0]["stderr"]
