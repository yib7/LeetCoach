"""Shared test helpers (audit P2-7 de-duplication).

`tests/` is not a package; pytest's default (prepend) import mode puts this
directory on ``sys.path``, so test modules can ``from _helpers import ...``.

Holds the pieces that were copy-pasted across several test files:

* :func:`parse_sse` — split a raw SSE response body into ``(text_chunks,
  events)``; four byte-for-byte-equivalent copies previously lived in
  ``test_web``, ``test_modes``, ``test_classifier_offpath`` and
  ``test_verify_detail``.
* :data:`CLASSIFY_JSON` — the default canned classifier reply
  (``two_pointers`` / ``["arrays"]``) those same modules fed to the fake Claude.
  Two test modules intentionally keep their OWN classifier fixture and are NOT
  unified here: ``test_topic_index`` asserts on ``binary_search`` and
  ``test_modes`` exercises a two-topic reply — importing this constant would
  break the first and hide the intent of the second.
"""
from __future__ import annotations

import json

# The default canned classifier JSON (matches what the real classifier prompt
# asks for: a tiny object naming the technique + topics).
CLASSIFY_JSON = {"problem_type": "two_pointers", "topics": ["arrays"]}


def parse_sse(body: str):
    """Split a raw SSE body into ``(text_chunks, events)`` where ``events`` is a
    list of ``(event_name, payload)``."""
    text_chunks = []
    events = []
    for block in body.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        event_name = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        data = "\n".join(data_lines)
        if event_name is None:
            # plain data: event => a text delta (json-encoded string)
            text_chunks.append(json.loads(data))
        else:
            events.append((event_name, json.loads(data) if data else None))
    return text_chunks, events


# Canned `claude --help` text for the A7 help-probe gate. The suite-wide
# autouse fixture (tests/conftest.py) feeds this to claude_cli's probe so no
# test ever spawns the real CLI just to learn which flags it supports. It lists
# every optional isolation flag LeetCoach knows about (and --bare, which must
# still never be passed).
FAKE_CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  --bare                                Minimal mode (never used by LeetCoach)
  --model <model>                       Model for the current session.
  --no-session-persistence              Disable session persistence
  --output-format <format>              Output format
  -p, --print                           Print response and exit
  --safe-mode                           Start with all customizations disabled
  --strict-mcp-config                   Only use MCP servers from --mcp-config
  --system-prompt <prompt>              System prompt to use for the session
  --tools <tools...>                    Use "" to disable all tools
  --verbose                             Override verbose mode setting
"""


def pid_alive(pid: int) -> bool:
    """True while process ``pid`` is still running (no signal is sent).

    Windows: ``os.kill(pid, 0)`` would call TerminateProcess, so ask the kernel
    via OpenProcess + WaitForSingleObject instead. POSIX: signal 0 probe.
    """
    import os
    import sys

    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        synchronize = 0x00100000
        handle = k32.OpenProcess(synchronize, 0, pid)
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid: int, timeout: float = 8.0) -> bool:
    """Poll until ``pid`` is gone (True) or ``timeout`` elapses (False)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)
