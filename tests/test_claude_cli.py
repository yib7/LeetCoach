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

import claude_cli

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
