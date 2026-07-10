"""Tests for the keystone `claude_cli` wrapper.

Every test injects a fake `runner` (or fake `which`) so NO real `claude`
subprocess is ever spawned. The fakes return canned stream-json lines whose
shape matches what the real `claude -p --output-format stream-json --verbose`
emits (observed live during SP1):

  - true streaming deltas come on lines:
      {"type":"stream_event","event":{"type":"content_block_delta",
       "delta":{"type":"text_delta","text":"..."}}}
  - thinking blocks arrive as signature_delta / thinking deltas that must be
    IGNORED (they are not user-visible answer text)
  - a fallback shape (when --include-partial-messages is off) is:
      {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
"""
from __future__ import annotations

import json
import sys
import threading

import claude_cli
import proc_util

# --- fakes ---------------------------------------------------------------

def make_recording_runner(lines):
    """Return (runner, calls) where runner records argv + stdin and yields `lines`."""
    calls = []

    def runner(argv, stdin_text):
        calls.append({"argv": list(argv), "stdin": stdin_text})
        for line in lines:
            yield line

    return runner, calls


def stream_event_line(text):
    return json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": text},
            },
        }
    )


def thinking_signature_line():
    return json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "abc=="},
            },
        }
    )


def assistant_text_line(text):
    return json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def result_line(text):
    return json.dumps({"type": "result", "subtype": "success", "result": text})


# --- (a) prompt goes to stdin, not argv ----------------------------------

def test_prompt_is_delivered_via_stdin_not_argv():
    runner, calls = make_recording_runner([stream_event_line("OK")])
    huge_prompt = "Solve this:\n" + ("x" * 50_000)

    list(claude_cli.run(huge_prompt, model="claude-opus-4-8", runner=runner))

    assert len(calls) == 1
    call = calls[0]
    # The prompt rides on stdin verbatim...
    assert call["stdin"] == huge_prompt
    # ...and must NOT appear as an argv argument (no arg-length/escaping limits).
    assert all(huge_prompt not in str(arg) for arg in call["argv"])


def test_argv_requests_stream_json_and_passes_model():
    runner, calls = make_recording_runner([stream_event_line("OK")])

    list(claude_cli.run("hi", model="my-model-id", runner=runner))

    argv = calls[0]["argv"]
    # -p / --print for non-interactive mode
    assert ("-p" in argv) or ("--print" in argv)
    # stream-json output is what the parser consumes
    assert "stream-json" in argv
    # the requested model is forwarded
    assert "my-model-id" in argv
    # stream-json requires --verbose on this CLI; the wrapper must add it
    assert "--verbose" in argv


def test_default_model_used_when_none_given(monkeypatch):
    monkeypatch.delenv("LEETCOACH_MODEL", raising=False)
    runner, calls = make_recording_runner([stream_event_line("OK")])

    list(claude_cli.run("hi", runner=runner))

    argv = calls[0]["argv"]
    assert "claude-opus-4-8" in argv


# --- (b) stream-json -> text-delta assembly ------------------------------

def test_stream_events_assemble_into_text_deltas():
    lines = [
        stream_event_line("Hello"),
        stream_event_line(", "),
        stream_event_line("world"),
    ]
    runner, _ = make_recording_runner(lines)

    deltas = list(claude_cli.run("hi", runner=runner))

    # each text_delta is yielded as its own chunk, in order
    assert deltas == ["Hello", ", ", "world"]
    # and they assemble into the full answer
    assert "".join(deltas) == "Hello, world"


def test_thinking_and_signature_deltas_are_ignored():
    lines = [
        thinking_signature_line(),          # must NOT surface as text
        stream_event_line("visible"),
        thinking_signature_line(),
    ]
    runner, _ = make_recording_runner(lines)

    deltas = list(claude_cli.run("hi", runner=runner))

    assert "".join(deltas) == "visible"


def test_non_text_and_noise_lines_are_skipped():
    lines = [
        '{"type":"system","subtype":"init","session_id":"x"}',
        "",                                  # blank line
        "not json at all",                   # garbage line, must not crash
        stream_event_line("answer"),
        result_line("answer"),               # result echoes full text; not double-counted
    ]
    runner, _ = make_recording_runner(lines)

    deltas = list(claude_cli.run("hi", runner=runner))

    assert "".join(deltas) == "answer"


def test_falls_back_to_assistant_message_when_no_stream_events():
    # When partial messages are off, text arrives only on assistant lines.
    lines = [
        '{"type":"system","subtype":"init"}',
        assistant_text_line("fallback text"),
        result_line("fallback text"),
    ]
    runner, _ = make_recording_runner(lines)

    deltas = list(claude_cli.run("hi", runner=runner))

    assert "".join(deltas) == "fallback text"


# --- (c) is_available true / false paths ---------------------------------

def test_is_available_true_when_binary_found():
    assert claude_cli.is_available(which=lambda name: "/usr/bin/claude") is True


def test_is_available_false_when_binary_missing():
    assert claude_cli.is_available(which=lambda name: None) is False


def test_is_available_uses_configured_binary_name(monkeypatch):
    monkeypatch.setenv("LEETCOACH_CLAUDE_BIN", "my-claude")
    seen = {}

    def fake_which(name):
        seen["name"] = name
        return "/somewhere/my-claude"

    assert claude_cli.is_available(which=fake_which) is True
    assert seen["name"] == "my-claude"


def test_run_raises_clear_error_when_unavailable():
    # If the wrapper is asked to run while claude is unavailable, the error
    # message must clearly name the missing dependency.
    def fake_which(name):
        return None

    try:
        list(claude_cli.run("hi", runner=None, which=fake_which))
    except claude_cli.ClaudeUnavailableError as exc:
        assert "claude" in str(exc).lower()
    else:
        raise AssertionError("expected ClaudeUnavailableError when claude is missing")


# --- (d) _real_runner: stderr never deadlocks the stdout stream -----------
# These drive the REAL subprocess path, but use this Python interpreter as a
# harmless stand-in for `claude` (a local helper script) — still no real
# `claude` call, no network. A regression here would deadlock, so each runs in
# a worker thread with a join timeout: a hang fails the assert instead of
# wedging the suite.

def _drive_real_runner(argv, stdin_text, timeout=20):
    out: dict = {}

    def go():
        try:
            out["lines"] = list(claude_cli._real_runner(argv, stdin_text))
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            out["exc"] = exc

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "_real_runner did not finish in time — stderr/stdout pipe deadlock?"
    if "exc" in out:
        raise out["exc"]
    return out["lines"]


def test_real_runner_large_stderr_does_not_deadlock():
    # The child floods stderr (~300KB, far past a 64KB OS pipe buffer) BEFORE
    # writing stdout. If stderr were an unread PIPE this would deadlock; with
    # stderr redirected to a file the run completes and stdout still arrives.
    script = (
        "import sys\n"
        "sys.stdin.read()\n"
        "sys.stderr.write('E' * 300000)\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write('hello\\n')\n"
        "sys.stdout.write('world\\n')\n"
    )
    lines = _drive_real_runner([sys.executable, "-c", script], "ping")
    assert [ln.strip() for ln in lines] == ["hello", "world"]


def test_real_runner_kills_subprocess_on_early_close():
    """Closing the generator early (the SSE client disconnected) must terminate
    the child instead of leaving it running to completion. Regression guard for
    audit P2 #4 (client disconnect should cancel the Claude subprocess).

    The child prints one line, then sleeps far longer than the test would wait.
    We read exactly one line, then close() the generator (as Flask does on
    disconnect) and assert the child actually exits quickly.
    """
    import time

    script = (
        "import sys, time\n"
        "sys.stdin.read()\n"
        "sys.stdout.write('first\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"          # would hang for 60s if not terminated
        "sys.stdout.write('second\\n')\n"
    )
    gen = claude_cli._real_runner([sys.executable, "-c", script], "ping")
    first = next(gen)
    assert first.strip() == "first"

    start = time.monotonic()
    gen.close()  # Flask does this on client disconnect (throws GeneratorExit)
    elapsed = time.monotonic() - start
    # If the child were left to run its sleep(60), close() would block on
    # proc.wait() for ~60s. Terminating it makes close() return promptly.
    assert elapsed < 15, f"early close took {elapsed:.1f}s — subprocess was not terminated"


def test_run_full_chain_kills_subprocess_on_early_close():
    """Same regression as test_real_runner_kills_subprocess_on_early_close, but
    drives the FULL production chain: claude_cli.run() -> _iter_text_deltas()
    -> _real_runner(), exactly as app.py's SSE generator does. Closing the
    generator returned by `run()` must still propagate down to `_real_runner`
    and terminate the underlying fake subprocess.

    Regression guard for audit review round 1, Important #1: GeneratorExit
    reaching `_real_runner` through `_iter_text_deltas`'s plain `for raw in
    lines:` was incidental (CPython refcounting-based `lines.close()` on GC),
    not guaranteed by the iterator protocol. `_iter_text_deltas` now closes
    `lines` explicitly in a `finally`, so this full-chain path must behave the
    same as calling `_real_runner` directly.
    """
    import time

    script = (
        "import sys, time\n"
        "sys.stdin.read()\n"
        "sys.stdout.write('{\\\"type\\\": \\\"stream_event\\\", \\\"event\\\": "
        "{\\\"type\\\": \\\"content_block_delta\\\", \\\"delta\\\": "
        "{\\\"type\\\": \\\"text_delta\\\", \\\"text\\\": \\\"first\\\"}}}\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"          # would hang for 60s if not terminated
        "sys.stdout.write('second\\n')\n"
    )

    def real_argv_runner(argv, stdin_text):
        # Ignore the real argv claude_cli.run() built (it targets `claude`);
        # substitute this python stand-in script instead, but otherwise go
        # through the exact same _real_runner code path.
        return claude_cli._real_runner([sys.executable, "-c", script], stdin_text)

    gen = claude_cli.run("hi", runner=real_argv_runner)
    first = next(gen)
    assert first == "first"

    start = time.monotonic()
    gen.close()  # Flask does this on client disconnect (throws GeneratorExit)
    elapsed = time.monotonic() - start
    assert elapsed < 15, f"early close took {elapsed:.1f}s — subprocess was not terminated"


def test_iter_text_deltas_closes_lines_on_early_generator_close():
    """Unit-level check (no subprocess at all): if the consumer of
    claude_cli._iter_text_deltas() closes it early, the underlying `lines`
    iterable's .close() must be invoked deterministically -- not left to GC.
    """
    closed = {"called": False}

    class FakeLines:
        def __iter__(self):
            return self

        def __next__(self):
            return stream_event_line("chunk")

        def close(self):
            closed["called"] = True

    gen = claude_cli._iter_text_deltas(FakeLines())
    first = next(gen)
    assert first == "chunk"
    gen.close()

    assert closed["called"] is True


def test_kill_process_tree_uses_taskkill_on_windows(monkeypatch):
    """Windows-specific: `claude` is typically an npm shim (claude.cmd -> node),
    so a plain proc.terminate() only kills the shim and leaks the real node
    child. On win32, _kill_process_tree must shell out to
    `taskkill /T /F /PID <pid>` instead of calling proc.terminate() directly.

    Regression guard for audit review round 1, Important #2. This monkeypatches
    both sys.platform and subprocess.run so no real process (and no real
    taskkill) is invoked. The implementation lives in `proc_util` (shared with
    sandbox.py since audit6 P1-2), so that's where the patches land; the call
    still goes through `claude_cli._kill_process_tree` to pin the re-export.
    """
    monkeypatch.setattr(proc_util.sys, "platform", "win32")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(proc_util.subprocess, "run", fake_run)

    class FakeProc:
        pid = 4242

        def terminate(self):
            raise AssertionError("terminate() must not be called when taskkill is used")

    invoked_tree_kill = claude_cli._kill_process_tree(FakeProc())

    assert invoked_tree_kill is True
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "taskkill"
    assert "/T" in cmd and "/F" in cmd
    assert "/PID" in cmd
    assert str(FakeProc.pid) in cmd


def test_kill_process_tree_falls_back_to_terminate_on_non_windows(monkeypatch):
    monkeypatch.setattr(proc_util.sys, "platform", "linux")
    terminated = {"called": False}

    class FakeProc:
        pid = 4242

        def terminate(self):
            terminated["called"] = True

    invoked_tree_kill = claude_cli._kill_process_tree(FakeProc())

    assert invoked_tree_kill is False
    assert terminated["called"] is True


def test_real_runner_surfaces_stderr_on_nonzero_exit():
    # A nonzero exit must still raise with the child's stderr text attached
    # (read back from the temp file), so diagnostics aren't lost.
    script = (
        "import sys\n"
        "sys.stdin.read()\n"
        "sys.stderr.write('boom diagnostic detail')\n"
        "sys.exit(3)\n"
    )
    try:
        _drive_real_runner([sys.executable, "-c", script], "ping")
    except claude_cli.ClaudeUnavailableError as exc:
        assert "boom diagnostic detail" in str(exc)
        assert "3" in str(exc)
    else:
        raise AssertionError("expected ClaudeUnavailableError on nonzero exit")
