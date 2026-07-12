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
gets rlimits (in ``sandbox._posix_limits``); the Windows analogue is a **Job
Object** (:func:`create_job_with_caps` pre-spawn, :func:`assign_to_job` right
after spawn, :func:`close_job` in cleanup), implemented via ctypes so no new
dependency (pywin32/psutil) is pulled in.
"""
from __future__ import annotations

import os
import subprocess
import sys


def kill_process_tree(proc: "subprocess.Popen[str]") -> bool:
    """Best-effort kill of `proc` AND its descendants. Returns True if a
    tree-kill mechanism was invoked (not necessarily that it succeeded).

    On Windows, ``taskkill /T`` walks the process tree by PID and kills the
    whole thing. No new dependency (psutil) is pulled in for this; taskkill
    ships with Windows. Falls back to terminate()/kill() on non-Windows or if
    taskkill itself fails to launch. Callers should still ``wait()`` on the
    process afterwards to reap it (and ``kill()`` as a last resort if the
    wait times out).
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
    if _job_api is None:
        return None
    try:
        job = _job_api.CreateJobObjectW(None, None)
        if not job:
            return None
        try:
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            info.BasicLimitInformation.ActiveProcessLimit = active_processes
            info.ProcessMemoryLimit = memory_bytes
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


def assign_to_job(job_handle, proc: "subprocess.Popen[str]") -> bool:
    """Assign a just-spawned ``proc`` to a job from :func:`create_job_with_caps`.

    This is the ONLY post-spawn step: one ``AssignProcessToJobObject``
    syscall, microseconds — everything slow (the one-time ctypes setup, job
    creation, limit configuration) already happened at module import /
    pre-spawn. Residual race, re-justified: the child is ``python
    solution.py``, which needs ~20ms+ of interpreter startup before it can
    execute a line of untrusted code, so one syscall cannot lose that race —
    the child can neither allocate nor spawn outside the job first. Closing
    the window completely would need ``CREATE_SUSPENDED``, and resuming
    requires the main thread id which ``subprocess.Popen`` doesn't expose —
    i.e. reimplementing CreateProcessW via ctypes or calling undocumented
    ``NtResumeProcess``, where a failed resume would wedge every run. One
    fast syscall against a ~20ms window is the robust trade, and
    :func:`kill_process_tree` (taskkill /T) remains the backstop.

    Returns True when the child is inside the job; False (``None`` handle,
    POSIX, or API failure) means the caller proceeds uncapped. Never raises.
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
    try:
        _job_api.CloseHandle(job_handle)
    except Exception:  # noqa: BLE001 - cleanup must never raise
        pass
