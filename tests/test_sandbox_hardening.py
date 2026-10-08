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


# --- B6: raw-byte capture, rmtree retry, stale-dir sweep --------------------


def test_stdout_is_not_lost_when_a_grandchild_holds_the_pipe(tmp_path):
    """B6: the solution prints its answer and exits 0, but a grandchild it
    spawned inherits stdout and keeps the pipe open. The old buffered
    ``read(8192)`` blocked waiting for 8 KB-or-EOF with the answer stuck in
    its buffer, so the bounded join gave up and the verdict saw EMPTY stdout
    (a false FAIL). Raw reads hand over whatever has arrived."""
    pidfile = tmp_path / "gc.pid"
    code = (
        "import subprocess, sys\n"
        "pidfile = sys.stdin.readline().strip()\n"
        "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                      stdout=sys.stdout, stderr=sys.stderr)\n"
        "open(pidfile, 'w').write(str(gc.pid))\n"
        "print(42)\n"
    )
    start = time.monotonic()
    r = sandbox.verify_python(code, str(pidfile) + "\n", "42", timeout=15, audit_hook=False)
    elapsed = time.monotonic() - start
    assert r.status == "pass", r
    assert elapsed < 10, f"took {elapsed:.1f}s -- blocked on the grandchild's pipe"
    if os.name == "nt" and pidfile.exists():
        gc_pid = int(pidfile.read_text())
        subprocess.run(["taskkill", "/F", "/PID", str(gc_pid)], capture_output=True, check=False)


def test_multibyte_output_split_across_reads_decodes_cleanly():
    """B6: bytes are decoded once at the end, so a UTF-8 character straddling
    a read boundary is never turned into replacement characters."""
    code = "print('é' * 20000)\n"   # 40 000 bytes: crosses many read boundaries
    r = sandbox.verify_python(code, "", "é" * 20000)
    assert r.status == "pass", r


def test_output_cap_is_enforced_in_bytes():
    code = "import sys\nsys.stdout.write('é' * 40000)\nsys.stdout.flush()\n"  # 80 000 bytes
    r = sandbox.verify_python(code, "", "x", timeout=15)
    assert r.status == "error", r
    assert "exceeded" in r.note
    kept = r.detail[0]["stdout"]
    assert len(kept.encode("utf-8")) <= sandbox._OUTPUT_LIMIT + 64


def test_rmtree_retries_with_backoff_until_the_dir_is_gone(tmp_path, monkeypatch):
    """B6: right after a kill, Windows can still hold a handle on a file in
    the run dir for a moment; one ignore_errors rmtree leaked the dir (98
    stale ``leetcoach_run_*`` dirs were found in %TEMP%)."""
    victim = tmp_path / "leetcoach_run_x"
    victim.mkdir()
    (victim / "solution.py").write_text("x", encoding="utf-8")
    real_rmtree = sandbox.shutil.rmtree
    calls = []
    sleeps = []

    def flaky(path, *a, **k):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError("[WinError 32] file in use")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(sandbox.shutil, "rmtree", flaky)
    monkeypatch.setattr(sandbox, "_sleep", sleeps.append)
    assert sandbox._rmtree_with_retry(str(victim)) is True
    assert not victim.exists()
    assert len(calls) == 3
    assert sleeps == sorted(sleeps) and sleeps[0] > 0  # backoff grows


def test_rmtree_retry_gives_up_quietly(tmp_path, monkeypatch):
    victim = tmp_path / "leetcoach_run_stuck"
    victim.mkdir()

    def always_locked(path, *a, **k):
        raise PermissionError("[WinError 32] file in use")

    monkeypatch.setattr(sandbox.shutil, "rmtree", always_locked)
    monkeypatch.setattr(sandbox, "_sleep", lambda s: None)
    assert sandbox._rmtree_with_retry(str(victim)) is False  # never raises


def test_verify_python_cleans_up_through_the_retrying_rmtree(monkeypatch):
    seen = []
    real = sandbox._rmtree_with_retry

    def spy(path, **k):
        seen.append(path)
        return real(path, **k)

    monkeypatch.setattr(sandbox, "_rmtree_with_retry", spy)
    r = sandbox.verify_python("print(1)\n", "", "1")
    assert r.status == "pass", r
    assert len(seen) == 1 and os.path.basename(seen[0]).startswith("leetcoach_run_")
    assert not os.path.exists(seen[0])


def _age(path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_sweep_removes_only_stale_leetcoach_run_dirs(tmp_path):
    """B6 startup sweep: only DIRECTORIES named ``leetcoach_run_*`` whose mtime
    is older than a day. Everything else in the temp dir is left alone."""
    stale = tmp_path / "leetcoach_run_stale"
    stale.mkdir()
    (stale / "solution.py").write_text("x", encoding="utf-8")
    _age(stale, 2)
    fresh = tmp_path / "leetcoach_run_fresh"
    fresh.mkdir()
    other = tmp_path / "someone_elses_dir"
    other.mkdir()
    _age(other, 30)
    lookalike_file = tmp_path / "leetcoach_run_file.txt"
    lookalike_file.write_text("keep", encoding="utf-8")
    _age(lookalike_file, 30)
    prefix_inside = tmp_path / "xleetcoach_run_old"
    prefix_inside.mkdir()
    _age(prefix_inside, 30)

    removed = sandbox.sweep_stale_run_dirs(tmp_root=str(tmp_path))

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists() and other.exists() and prefix_inside.exists()
    assert lookalike_file.read_text(encoding="utf-8") == "keep"


def test_sweep_never_follows_a_symlinked_lookalike(tmp_path):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "data.txt").write_text("keep", encoding="utf-8")
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    link = temp_root / "leetcoach_run_link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    _age(target, 5)
    removed = sandbox.sweep_stale_run_dirs(tmp_root=str(temp_root))
    assert removed == 0
    assert (target / "data.txt").read_text(encoding="utf-8") == "keep"


def test_sweep_defaults_to_the_system_temp_dir_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / "leetcoach_run_old"
    stale.mkdir()
    _age(stale, 3)
    assert sandbox.sweep_stale_run_dirs() == 1
    assert not stale.exists()
    assert sandbox.sweep_stale_run_dirs(tmp_root=str(tmp_path / "missing")) == 0


def test_app_startup_sweeps_stale_run_dirs(monkeypatch):
    import app as app_module

    calls = []
    monkeypatch.setattr(sandbox, "sweep_stale_run_dirs", lambda **k: calls.append(k) or 4)
    assert app_module._sweep_sandbox_temp() == 4
    assert calls

    def boom(**k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(sandbox, "sweep_stale_run_dirs", boom)
    assert app_module._sweep_sandbox_temp() == 0  # a hiccup never blocks launch

    src = open(app_module.__file__, encoding="utf-8").read()
    main_block = src[src.index('if __name__ == "__main__":'):]
    assert "_sweep_sandbox_temp()" in main_block


# --- B6 review: raw reads, decode-at-end, cleanup after kill, sweep safety --


def _reader_on_pipe():
    """A _CappedReader draining the read end of a fresh OS pipe; returns
    (reader, write_fd)."""
    r_fd, w_fd = os.pipe()
    reader = sandbox._CappedReader(open(r_fd, "rb"))
    return reader, w_fd


def _wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_capped_reader_hands_over_partial_output_while_the_pipe_stays_open():
    """The core B6 property, without any subprocess: a few bytes written to a
    pipe whose write end is still open (a grandchild holding it) are visible
    at once. The old buffered ``read(8192)`` sat on them until 8 KB or EOF."""
    reader, w_fd = _reader_on_pipe()
    try:
        os.write(w_fd, b"42\n")
        assert _wait_for(lambda: reader.text() == "42\n"), reader.text()
        assert reader.is_alive()  # still draining: no EOF yet
    finally:
        os.close(w_fd)
    reader.join(timeout=5)
    assert not reader.is_alive()


def test_capped_reader_decodes_once_at_the_end():
    """A UTF-8 character split across two writes decodes cleanly, and bytes
    that are not UTF-8 become U+FFFD instead of raising."""
    reader, w_fd = _reader_on_pipe()
    e_acute = "é".encode("utf-8")
    os.write(w_fd, b"a" + e_acute[:1])
    assert _wait_for(lambda: reader._kept == 2)  # first half already read
    os.write(w_fd, e_acute[1:] + b"b\xff\xfe")
    os.close(w_fd)
    reader.join(timeout=5)
    assert reader.text() == "aéb��"


def test_invalid_utf8_from_the_solution_is_replaced_not_raised():
    code = "import sys\nsys.stdout.buffer.write(b'ok\\xff\\n')\n"
    r = sandbox.verify_python(code, "", "ok")
    assert r.status == "fail", r  # compared, not crashed
    assert r.detail[0]["stdout"].startswith("ok�"), r


def test_grandchild_output_is_captured_even_when_it_outlives_the_child(tmp_path):
    """A grandchild inherits stdout, prints its own line, and then keeps the
    pipe open after the solution exited. Both lines reach the verdict and the
    run still finishes promptly (the straggler is killed via the job)."""
    ready = tmp_path / "gc.ready"
    gc_src = (
        "import sys, time; print('from grandchild', flush=True); "
        f"open({str(ready)!r}, 'w').close(); time.sleep(60)"
    )
    code = (
        "import subprocess, sys, os, time\n"
        "print('from child', flush=True)\n"
        f"subprocess.Popen([sys.executable, '-c', {gc_src!r}])\n"
        f"while not os.path.exists({str(ready)!r}): time.sleep(0.02)\n"
    )
    start = time.monotonic()
    r = sandbox.verify_python(
        code, "", "from child\nfrom grandchild", timeout=20, audit_hook=False
    )
    assert r.status == "pass", r
    assert time.monotonic() - start < 10


def test_go_byte_is_released_only_after_the_job_assignment(monkeypatch):
    """A5 ordering, checked at the call level: the stdin feeder (which sends
    the go byte) starts strictly after ``assign_to_job`` returned."""
    events: list = []
    real_assign, real_feed = sandbox.assign_to_job, sandbox._feed_stdin

    def assign(job, proc):
        events.append("assign")
        return real_assign(job, proc)

    def feed(pipe, data):
        events.append(("feed", data[:1]))
        return real_feed(pipe, data)

    monkeypatch.setattr(sandbox, "assign_to_job", assign)
    monkeypatch.setattr(sandbox, "_feed_stdin", feed)
    r = sandbox.verify_python("print(input())\n", "7\n", "7")
    assert r.status == "pass", r
    assert events == ["assign", ("feed", sandbox._GO)]


def _record_run_dirs(monkeypatch) -> list:
    dirs: list = []
    real = sandbox.tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        dirs.append(d)
        return d

    monkeypatch.setattr(sandbox.tempfile, "mkdtemp", spy)
    return dirs


def test_run_dir_is_removed_after_a_timeout_kill_with_a_file_held_open(monkeypatch):
    """B6 cleanup after a kill: a grandchild holds a file in the run dir open
    and the solution never finishes. The tree is killed on timeout and the
    run dir is still gone afterwards (retry/backoff absorbs the moment
    Windows keeps the handle while the processes are torn down)."""
    dirs = _record_run_dirs(monkeypatch)
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c',\n"
        "    'import time; f = open(\"held.txt\", \"w\"); f.write(\"x\"); f.flush(); "
        "time.sleep(60)'])\n"
        "open('mine.txt', 'w')\n"
        "time.sleep(60)\n"
    )
    r = sandbox.verify_python(code, "", "x", timeout=2, audit_hook=False)
    assert r.status == "error" and "timed out" in r.note, r
    assert len(dirs) == 1
    assert not os.path.exists(dirs[0]), "run dir leaked after the kill"


def test_run_dir_is_removed_even_with_a_read_only_file_inside(monkeypatch):
    dirs = _record_run_dirs(monkeypatch)
    code = (
        "import os, stat\n"
        "open('ro.txt', 'w').write('x')\n"
        "os.chmod('ro.txt', stat.S_IREAD)\n"
        "print('ok')\n"
    )
    r = sandbox.verify_python(code, "", "ok")
    assert r.status == "pass", r
    assert not os.path.exists(dirs[0])


@pytest.mark.parametrize(
    "failure", [PermissionError("[WinError 32] in use"), RuntimeError("surprise")]
)
def test_a_cleanup_failure_never_reaches_the_verdict(monkeypatch, failure):
    """Whatever ``rmtree`` throws, the verdict stands (the cleanup runs in a
    ``finally``: an escaping exception would replace the return value)."""
    dirs = _record_run_dirs(monkeypatch)
    real_rmtree = sandbox.shutil.rmtree

    def broken(path, *a, **k):
        raise failure

    monkeypatch.setattr(sandbox.shutil, "rmtree", broken)
    monkeypatch.setattr(sandbox, "_sleep", lambda s: None)
    try:
        r = sandbox.verify_python("print(1)\n", "", "1")
        assert r.status == "pass", r
    finally:
        monkeypatch.setattr(sandbox.shutil, "rmtree", real_rmtree)
        for d in dirs:
            real_rmtree(d, ignore_errors=True)


def test_rmtree_retry_keeps_going_when_an_inner_entry_vanished(tmp_path, monkeypatch):
    """A FileNotFoundError about something INSIDE the dir is not "the dir is
    gone": the retry must only report success once the path itself no longer
    exists."""
    victim = tmp_path / "leetcoach_run_inner"
    victim.mkdir()
    real_rmtree = sandbox.shutil.rmtree
    calls: list = []

    def flaky(path, *a, **k):
        calls.append(path)
        if len(calls) == 1:
            raise FileNotFoundError(2, "vanished", os.path.join(path, "child"))
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(sandbox.shutil, "rmtree", flaky)
    monkeypatch.setattr(sandbox, "_sleep", lambda s: None)
    assert sandbox._rmtree_with_retry(str(victim)) is True
    assert len(calls) == 2
    assert not victim.exists()


def _make_junction(link, target) -> None:
    import _winapi  # noqa: PLC0415 - Windows-only

    _winapi.CreateJunction(str(target), str(link))


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_sweep_never_follows_a_junction(tmp_path):
    """Junctions need no privilege on Windows, so unlike the symlink test this
    always runs here: a ``leetcoach_run_*`` junction is skipped outright, and
    a junction INSIDE a stale run dir is unlinked without touching what it
    points at."""
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "data.txt").write_text("keep", encoding="utf-8")
    temp_root = tmp_path / "temp"
    temp_root.mkdir()

    top_link = temp_root / "leetcoach_run_junction"
    _make_junction(top_link, precious)
    stale = temp_root / "leetcoach_run_stale"
    stale.mkdir()
    _make_junction(stale / "inner", precious)
    _age(stale, 3)
    _age(precious, 5)

    removed = sandbox.sweep_stale_run_dirs(tmp_root=str(temp_root))

    assert removed == 1
    assert not stale.exists()
    assert os.path.lexists(top_link)
    assert (precious / "data.txt").read_text(encoding="utf-8") == "keep"


def test_sweep_logs_an_undeletable_dir_and_carries_on(tmp_path, monkeypatch, caplog):
    stuck = tmp_path / "leetcoach_run_stuck"
    gone = tmp_path / "leetcoach_run_gone"
    for d in (stuck, gone):
        d.mkdir()
        _age(d, 3)
    real = sandbox._rmtree_with_retry

    def selective(path, **k):
        return False if path.endswith("stuck") else real(path, **k)

    monkeypatch.setattr(sandbox, "_rmtree_with_retry", selective)
    with caplog.at_level("WARNING", logger="sandbox"):
        assert sandbox.sweep_stale_run_dirs(tmp_root=str(tmp_path)) == 1
    assert stuck.exists() and not gone.exists()
    assert any("leetcoach_run_stuck" in rec.getMessage() for rec in caplog.records)


def test_sweep_survives_an_entry_that_errors(tmp_path, monkeypatch):
    """A per-entry surprise (not just OSError) is swallowed; the sweep still
    returns a count and never raises."""
    stale = tmp_path / "leetcoach_run_a"
    stale.mkdir()
    _age(stale, 3)

    def boom(path, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(sandbox, "_rmtree_with_retry", boom)
    assert sandbox.sweep_stale_run_dirs(tmp_root=str(tmp_path)) == 0
