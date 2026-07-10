"""Shared subprocess helpers: whole-tree termination.

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
"""
from __future__ import annotations

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
