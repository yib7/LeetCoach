"""Shared subprocess helpers: whole-tree termination + Windows Job Object caps.

Why this module exists: two places in LeetCoach must kill a child process AND
everything it spawned, and a plain ``proc.terminate()`` cannot do that on
Windows:

* ``claude_cli`` — the `claude` binary is typically an npm shim (``claude.cmd``
  launching node as a child), so terminating the shim leaks the node process
  that is doing the actual work (and burning subscription usage).
* ``sandbox`` — the Answer-mode verifier runs **untrusted, LLM-generated**
  code; on a timeout the direct child dies but any grandchildren it spawned
  would survive and keep running on the host.

Both need the same primitive, so it lives here rather than being copy-pasted.

The sandbox additionally needs *resource caps* for its untrusted child. POSIX
gets rlimits (set by ``sandbox_bootstrap.apply_rlimits``); the Windows analogue is a **Job
Object** (:func:`create_job_with_caps` pre-spawn, :func:`assign_to_job` right
after spawn, :func:`close_job` in cleanup), implemented via ctypes so no new
dependency (pywin32/psutil) is pulled in.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys


def _taskkill_exe() -> str:
    """``%SystemRoot%\\System32\\taskkill.exe`` (3A C14). A bare ``taskkill``
    goes through CreateProcess's search order, which tries the application
    directory and the current directory before System32 - a planted
    ``taskkill.exe`` there would run instead. The bare name is only the
    fallback when ``SystemRoot`` is unset."""
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
    if root:
        return os.path.join(root, "System32", "taskkill.exe")
    return "taskkill"


def kill_process_tree(proc: subprocess.Popen[str], *, group: bool = False) -> bool:
    """Best-effort kill of `proc` AND its descendants. Returns True if a
    tree-kill mechanism was invoked (not necessarily that it succeeded).

    On Windows, ``taskkill /T`` walks the process tree by PID and kills the
    whole thing. No new dependency (psutil) is pulled in for this; taskkill
    ships with Windows. Falls back to terminate()/kill() on non-Windows or if
    taskkill itself fails to launch. Callers should still ``wait()`` on the
    process afterwards to reap it (and ``kill()`` as a last resort if the
    wait times out).

    ``group=True`` (C5, POSIX only) is for a child spawned with
    ``start_new_session=True``: its pid is also its process-group id, so
    ``killpg`` takes down every descendant still in that group — including
    ones that outlived the direct child. Only pass it for a child you started
    in its own session; otherwise the pid is not a group you own.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                [_taskkill_exe(), "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=10,
            )
            return True
        except (OSError, subprocess.TimeoutExpired):
            pass  # taskkill missing/unusable/hung — fall through to terminate()
    elif group and hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return True
        except (OSError, AttributeError):
            pass  # group already gone / not ours — fall through to terminate()
    try:
        proc.terminate()
    except OSError:
        pass  # already exited
    return False


def child_exited_unreaped(pid: int) -> bool | None:
    """POSIX: has our child ``pid`` exited? Asked WITHOUT reaping it
    (``waitid`` + ``WNOWAIT``), so its pid - and, for a child started with
    ``start_new_session``, its process-group id - stays reserved until the
    caller reaps it (3A C3).

    ``True`` once it exited (a zombie), ``False`` while it runs, ``None`` when
    this cannot be told without reaping: no ``waitid``/``WNOWAIT`` (Windows,
    macOS before Python 3.13) or the pid is not an unreaped child of ours.
    Never raises.
    """
    waitid = getattr(os, "waitid", None)
    wnowait = getattr(os, "WNOWAIT", None)
    if waitid is None or wnowait is None:
        return None
    try:
        info = waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | wnowait)
    except OSError:  # ChildProcessError: already reaped / not ours
        return None
    return info is not None  # None: WNOHANG and nothing exited yet


def kill_process_group(pgid: int) -> bool:
    """POSIX: SIGKILL every process in group ``pgid`` (3A C3). Only for the
    group of a child you started with ``start_new_session`` and whose pid is
    still reserved (alive, an unreaped zombie, or a group with members left).
    True if the signal was sent; a group that is already gone, or one we may
    not signal, is tolerated. Never raises."""
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        return False
    try:
        killpg(pgid, getattr(signal, "SIGKILL", 9))
    except OSError:  # ProcessLookupError / PermissionError
        return False
    return True


# --- Windows Job Object caps (audit6 P1-2 step 2) --------------------------
#
# ALL ctypes machinery — imports, structure definitions, kernel32 bindings —
# is set up at MODULE IMPORT time, not lazily inside the helpers. This is
# load-bearing, not style: the first in-process `import ctypes` + WinDLL
# binding costs ~100ms, and an earlier revision paid it AFTER Popen, inside
# the assignment helper — so the FIRST child of a fresh process (exactly the
# Flask app's first verification) finished interpreter startup and ran its
# untrusted code before any cap existed (cold-start race, caught at
# checkpoint verification; a warm pytest process never saw it because earlier
# tests pre-import ctypes). Import-time setup pays the cost long before any
# child exists, and the split into create_job_with_caps (pre-spawn) /
# assign_to_job (post-spawn) leaves exactly ONE syscall after the spawn.
# A setup failure here must not break the app import: it degrades to
# _job_api = None and the helpers no-op (uncapped run, never an error).

_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x0008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JOBOBJECTINFOCLASS value

_job_api = None          # configured kernel32, or None -> job caps unavailable
_ExtendedLimits = None   # JOBOBJECT_EXTENDED_LIMIT_INFORMATION structure

if os.name == "nt":
    try:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):  # IO_COUNTERS
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _BasicLimits(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),  # ULONG_PTR
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtLimits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
            _fields_ = [
                ("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.CreateJobObjectW.restype = wintypes.HANDLE
        _k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        _k32.SetInformationJobObject.restype = wintypes.BOOL
        _k32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        _k32.AssignProcessToJobObject.restype = wintypes.BOOL
        _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        _k32.CloseHandle.restype = wintypes.BOOL
        _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        _k32.TerminateJobObject.restype = wintypes.BOOL
        _k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)

        _job_api = _k32
        _ExtendedLimits = _ExtLimits
    except Exception:  # noqa: BLE001 - degrade to uncapped, never break import
        _job_api = None
        _ExtendedLimits = None


def create_job_with_caps(
    *,
    memory_bytes: int = 512 * 1024 * 1024,
    active_processes: int = 16,
):
    """Windows only: create and fully configure an anonymous Job Object —
    call this BEFORE spawning the child — carrying three limits:

    * ``JOB_OBJECT_LIMIT_PROCESS_MEMORY`` — per-process commit cap
      (``memory_bytes``), the Windows analogue of the POSIX ``RLIMIT_AS``;
    * ``JOB_OBJECT_LIMIT_ACTIVE_PROCESS`` — at most ``active_processes``
      simultaneous processes in the job (a fork bomb hits a wall instead of
      the host);
    * ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` — closing the returned handle
      terminates everything still inside the job, so :func:`close_job` in the
      caller's ``finally`` is an airtight second kill mechanism alongside
      :func:`kill_process_tree`.

    Creating + configuring pre-spawn means every slow step happens while no
    child exists; the only post-spawn work is the single syscall in
    :func:`assign_to_job`. Returns the job HANDLE — keep it alive for the
    child's whole lifetime, then hand it to :func:`close_job` — or ``None``
    on POSIX / when any job API fails (pre-Windows-8 can't nest jobs;
    unexpected environments), which means "proceed without caps": a cap is
    defense-in-depth and must never break a verification run. Never raises.
    """
    return _create_job(memory_bytes=memory_bytes, active_processes=active_processes)


def create_kill_on_close_job():
    """Windows only: a Job Object with ONLY ``KILL_ON_JOB_CLOSE`` — no memory
    or process-count caps (A6).

    For the ``claude`` CLI: it is trusted, needs far more than the sandbox's
    512 MB, and spawns helpers, so caps would break it. What it does need is a
    kill switch that reaches descendants even after the direct child exited
    (``taskkill /T`` walks parent pids, so it cannot find a grandchild whose
    parent is already gone — e.g. one still holding the stdout pipe open).
    :func:`terminate_job` / :func:`close_job` provide that. Same lifecycle and
    graceful-degradation contract as :func:`create_job_with_caps`: ``None`` on
    POSIX or any API failure, never raises.
    """
    return _create_job(memory_bytes=None, active_processes=None)


def _create_job(*, memory_bytes, active_processes):
    """Shared Job Object factory: KILL_ON_JOB_CLOSE always, plus the memory /
    process caps when given (``None`` = no such cap)."""
    if _job_api is None:
        return None
    try:
        job = _job_api.CreateJobObjectW(None, None)
        if not job:
            return None
        try:
            info = _ExtendedLimits()
            flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if active_processes is not None:
                flags |= _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                info.BasicLimitInformation.ActiveProcessLimit = active_processes
            if memory_bytes is not None:
                flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
                info.ProcessMemoryLimit = memory_bytes
            info.BasicLimitInformation.LimitFlags = flags
            ok = _job_api.SetInformationJobObject(
                job,
                _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                raise OSError("SetInformationJobObject failed")
        except BaseException:
            _job_api.CloseHandle(job)  # no orphaned handle on any failure path
            raise
        return job
    except Exception:  # noqa: BLE001 - graceful degradation: run uncapped
        return None


def assign_to_job(job_handle, proc: subprocess.Popen[str]) -> bool:
    """Assign a just-spawned ``proc`` to a job from :func:`create_job_with_caps`.

    One ``AssignProcessToJobObject`` syscall — everything slow (the one-time
    ctypes setup, job creation, limit configuration) already happened at
    module import / pre-spawn. The call itself is NOT a race guard: an
    earlier revision relied on "one syscall beats ~20ms of interpreter
    startup", and GIL contention in the parent (other busy Python threads
    delay this thread several switch intervals) let a 700 MB allocation
    escape the cap in 19/20 runs. The sandbox therefore makes the child wait
    for a go byte that is only sent after this returns (SP3 A5, see
    ``sandbox_bootstrap.py``), and spawns the real interpreter rather than a
    venv launcher stub whose grandchild would start outside the job.
    ``CREATE_SUSPENDED`` is still avoided: resuming needs the main thread id
    that ``subprocess.Popen`` doesn't expose.

    Returns True when the child is inside the job; False (``None`` handle,
    POSIX, or API failure) means the child is NOT capped: ``claude_cli``
    proceeds without the kill-on-close job, while the sandbox fails closed
    (kills the child before its go byte). Never raises.
    """
    if job_handle is None or _job_api is None:
        return False
    try:
        # CPython's Windows Popen keeps the CreateProcess handle (opened with
        # PROCESS_ALL_ACCESS) in `_handle` — private but stable across
        # versions, and the graceful-degradation contract covers us if it
        # ever moves.
        proc_handle = getattr(proc, "_handle", None)
        if proc_handle is None:
            return False
        return bool(_job_api.AssignProcessToJobObject(job_handle, int(proc_handle)))
    except Exception:  # noqa: BLE001 - graceful degradation: run uncapped
        return False


def close_job(job_handle) -> None:
    """Close a Job Object handle from :func:`create_job_with_caps` (``None``
    is a no-op). With ``KILL_ON_JOB_CLOSE`` set, closing the last handle also
    terminates anything still running inside the job. Never raises."""
    if not job_handle or _job_api is None:
        return
    with contextlib.suppress(Exception):  # cleanup must never raise
        _job_api.CloseHandle(job_handle)


def terminate_job(job_handle) -> bool:
    """Kill every process currently inside the job, without closing the
    handle (``None`` is a no-op returning False). Used by a watchdog that must
    stop the whole ``claude`` tree mid-run; the owner still calls
    :func:`close_job` in its cleanup. Never raises."""
    if not job_handle or _job_api is None:
        return False
    try:
        return bool(_job_api.TerminateJobObject(job_handle, 1))
    except Exception:  # noqa: BLE001 - a kill helper must never raise
        return False
