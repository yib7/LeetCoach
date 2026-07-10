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
Object** (:func:`assign_job_with_caps` / :func:`close_job`), implemented via
ctypes so no new dependency (pywin32/psutil) is pulled in.
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

def assign_job_with_caps(
    proc: "subprocess.Popen[str]",
    *,
    memory_bytes: int = 512 * 1024 * 1024,
    active_processes: int = 16,
):
    """Windows only: cap ``proc`` — and everything it spawns — with an
    anonymous Job Object carrying three limits:

    * ``JOB_OBJECT_LIMIT_PROCESS_MEMORY`` — per-process commit cap
      (``memory_bytes``), the Windows analogue of the POSIX ``RLIMIT_AS``;
    * ``JOB_OBJECT_LIMIT_ACTIVE_PROCESS`` — at most ``active_processes``
      simultaneous processes in the job (a fork bomb hits a wall instead of
      the host);
    * ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` — closing the returned handle
      terminates everything still inside the job, so :func:`close_job` in the
      caller's ``finally`` is an airtight second kill mechanism alongside
      :func:`kill_process_tree`.

    Returns the job HANDLE — keep it alive for the child's whole lifetime,
    then hand it to :func:`close_job` — or ``None`` on POSIX or when any job
    API call fails (pre-Windows-8 can't nest jobs; unexpected environments).
    ``None`` means "proceed without caps": a cap is defense-in-depth and must
    never break a verification run. Never raises.

    Spawn-race note: the child is assigned *after* a plain spawn rather than
    created suspended. The assignment runs within microseconds of
    ``CreateProcess`` returning, while the child — ``python solution.py`` —
    is still deep in interpreter startup (python3xx.dll loading, ~tens of ms
    before user code can execute), so untrusted code cannot spawn a
    grandchild outside the job first. Doing better would mean
    ``CREATE_SUSPENDED``, and resuming needs the main thread id which
    ``subprocess.Popen`` doesn't expose — i.e. reimplementing CreateProcessW
    via ctypes or calling undocumented ``NtResumeProcess``, where a failed
    resume would wedge every run. The simpler path is also the more robust
    one here, and ``kill_process_tree`` (taskkill /T) remains the backstop
    for anything theoretically spawned pre-assignment.
    """
    if os.name != "nt":
        return None
    try:
        return _assign_job_nt(proc, memory_bytes, active_processes)
    except Exception:  # noqa: BLE001 - graceful degradation: run uncapped
        return None


def _assign_job_nt(proc, memory_bytes: int, active_processes: int):
    """The ctypes body of :func:`assign_job_with_caps` (nt only, may raise)."""
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):  # IO_COUNTERS
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class BasicLimits(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
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

    class ExtendedLimits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_limit_active_process = 0x0008
    job_object_limit_process_memory = 0x0100
    job_object_limit_kill_on_job_close = 0x2000
    job_object_extended_limit_information = 9  # JOBOBJECTINFOCLASS

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    # CPython's Windows Popen keeps the CreateProcess handle (opened with
    # PROCESS_ALL_ACCESS) in `_handle` — private but stable across versions,
    # and the graceful-degradation wrapper covers us if it ever moves.
    proc_handle = getattr(proc, "_handle", None)
    if proc_handle is None:
        return None

    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    try:
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = (
            job_object_limit_active_process
            | job_object_limit_process_memory
            | job_object_limit_kill_on_job_close
        )
        info.BasicLimitInformation.ActiveProcessLimit = active_processes
        info.ProcessMemoryLimit = memory_bytes
        ok = k32.SetInformationJobObject(
            job,
            job_object_extended_limit_information,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            raise OSError("SetInformationJobObject failed")
        if not k32.AssignProcessToJobObject(job, int(proc_handle)):
            raise OSError("AssignProcessToJobObject failed")
    except BaseException:
        k32.CloseHandle(job)  # no orphaned job handle on any failure path
        raise  # -> assign_job_with_caps returns None
    return job


def close_job(job_handle) -> None:
    """Close a Job Object handle from :func:`assign_job_with_caps` (``None``
    is a no-op). With ``KILL_ON_JOB_CLOSE`` set, closing the last handle also
    terminates anything still running inside the job. Never raises."""
    if not job_handle:
        return
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32")
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        k32.CloseHandle.restype = ctypes.c_int
        k32.CloseHandle(job_handle)
    except Exception:  # noqa: BLE001 - cleanup must never raise
        pass
