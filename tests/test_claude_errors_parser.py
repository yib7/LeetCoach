"""B2 / B3: truthful `claude` failure messages and a parser that cannot crash.

B2 - every nonzero exit used to be headlined "you are most likely signed out",
including usage limits, a bad model and prompt-too-long. The headline now comes
from the real error text; the sign-in guidance appears only when that text
carries an auth marker.

B3 - the stream-json parser crashed on odd event shapes (``text: null``,
non-dict events) and an ``is_error`` result with exit code 0 was passed off as
the answer. Every field is now type-guarded (fuzzed below) and an error result
raises.
"""
from __future__ import annotations

import json
import random
import sys

import pytest

import claude_cli


def _deltas(lines):
    return list(claude_cli._iter_text_deltas(lines))


def _text_line(text):
    return json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})


# --- B2: failure headline ---------------------------------------------------

@pytest.mark.parametrize("stderr", [
    "Claude AI usage limit reached|1760000000",
    "Error: invalid model name 'claude-nope'",
    "Prompt is too long",
    "node: internal error in module xyz",
])
def test_non_auth_failures_are_not_headlined_as_signed_out(stderr):
    msg = claude_cli.failure_message(1, stderr)
    lowered = msg.lower()
    assert "signed out" not in lowered
    assert "auth login" not in lowered
    headline = msg.splitlines()[0]
    assert stderr.split("|")[0] in headline  # the real reason leads
    assert "1" in headline  # exit code still named


@pytest.mark.parametrize("detail", [
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "Invalid API key · Please run /login",
    "Not logged in. Please run claude auth login",
    "API Error: 401 {\"type\":\"error\",\"error\":{\"type\":\"authentication_error\"}}",
])
def test_auth_failures_carry_sign_in_guidance(detail):
    msg = claude_cli.failure_message(1, detail)
    assert detail.splitlines()[0][:40] in msg
    assert "claude auth login" in msg
    assert "sign in" in msg.lower()


def test_failure_without_any_detail_says_so_without_guessing():
    msg = claude_cli.failure_message(3, "")
    assert "code 3" in msg
    assert "signed out" not in msg.lower()
    assert "no error details" in msg.lower()


def test_usage_limit_gets_a_specific_hint():
    msg = claude_cli.failure_message(1, "Claude AI usage limit reached")
    assert "usage limit" in msg.lower()
    assert "signed out" not in msg.lower()


@pytest.mark.parametrize("detail", [
    "You've hit your limit \u00b7 resets 3pm (Europe/London)",
    "5-hour limit reached \u2219 resets 7pm",
    "Claude AI usage limit reached|1760000000",
    "API Error: 429 rate limit exceeded",
])
def test_limit_wordings_get_the_limit_hint(detail):
    # SP2 M3: the CLI's current wordings, not only "usage limit".
    assert claude_cli._hint_for(detail) == claude_cli._LIMIT_HINT


@pytest.mark.parametrize("detail", [
    # a stack trace column is not an HTTP 401
    "TypeError: x is undefined\n    at run (file:///C:/npm/claude/cli.js:401:12)",
    "at Object.<anonymous> (cli.js:12:401)",
    # "log in" / "sign in" inside other words
    "Error: tool catalog in cache is corrupt",
    "invalid design in prompt template",
])
def test_non_auth_text_does_not_trigger_the_sign_in_hint(detail):
    assert claude_cli._hint_for(detail) != claude_cli._SIGN_IN_HINT


@pytest.mark.parametrize("detail", [
    "API Error: 401 Unauthorized",
    "Request failed with status code 401",
    "Please log in again",
    "You must sign in to continue",
    "run /login",
])
def test_real_auth_markers_still_trigger_the_sign_in_hint(detail):
    assert claude_cli._hint_for(detail) == claude_cli._SIGN_IN_HINT


@pytest.mark.parametrize("detail", [
    # trailing punctuation after the status code (not a stack-trace column)
    "Request failed with status code 401.",
    "API Error: 401: Unauthorized",
])
def test_auth_status_code_followed_by_punctuation_still_triggers_the_hint(detail):
    # SP2 M6 review: the old lookaround `(?![\w:.])` rejected a bare trailing
    # "." or ":" after the code, so real CLI wordings like "...401." or
    # "401: Unauthorized" lost the hint. A stack-trace column (":401:12" -
    # colon/period followed by ANOTHER digit) must still be excluded.
    assert claude_cli._hint_for(detail) == claude_cli._SIGN_IN_HINT


@pytest.mark.parametrize("detail", [
    "HTTP 429: Too Many Requests",
    "Error 429.",
])
def test_limit_status_code_followed_by_punctuation_still_triggers_the_hint(detail):
    assert claude_cli._hint_for(detail) == claude_cli._LIMIT_HINT


@pytest.mark.parametrize("detail", [
    "TypeError: x is undefined\n    at run (file:///C:/npm/claude/cli.js:401:12)",
    "at Object.<anonymous> (cli.js:12:429)",
])
def test_status_code_stack_trace_column_with_trailing_digit_still_excluded(detail):
    # Regression guard for the punctuation fix above: a stack-trace column
    # (code immediately followed by ":<digits>") must still not match.
    assert claude_cli._hint_for(detail) not in (claude_cli._SIGN_IN_HINT, claude_cli._LIMIT_HINT)


@pytest.mark.parametrize("detail", [
    "context window limit reached",
    "max output token limit reached",
])
def test_generic_limit_reached_without_a_known_wording_is_not_a_usage_limit(detail):
    # SP2 M6 review: bare "limit reached" was too broad and mislabeled other
    # kinds of limits (context window, max output tokens) as a usage limit.
    assert claude_cli._hint_for(detail) != claude_cli._LIMIT_HINT


@pytest.mark.parametrize("detail", [
    "usage limit reached",
    "5-hour limit reached",
    "3 hour limit reached",
    "You've hit your limit for today",
])
def test_known_limit_reached_wordings_still_trigger_the_limit_hint(detail):
    assert claude_cli._hint_for(detail) == claude_cli._LIMIT_HINT


def test_real_runner_nonzero_exit_headline_is_the_stderr_text():
    script = (
        "import sys\n"
        "sys.stdin.read()\n"
        "sys.stderr.write('Error: model claude-nope not found\\n')\n"
        "sys.exit(1)\n"
    )
    with pytest.raises(claude_cli.ClaudeUnavailableError) as info:
        list(claude_cli._real_runner([sys.executable, "-c", script], "ping"))
    msg = str(info.value)
    assert "model claude-nope not found" in msg.splitlines()[0]
    assert "signed out" not in msg.lower()


def test_stdout_auth_error_with_nonzero_exit_surfaces_through_run():
    """`claude` reports an auth failure on STDOUT (stderr empty) and exits
    nonzero. Through the production path (run -> parser -> real runner) the
    parser raises on the is_error result before the exit code is looked at
    (3A C4), so the real reason and the sign-in hint still reach the user."""
    script = (
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':True,"
        "'result':'Failed to authenticate: OAuth session expired'}))\n"
        "sys.exit(1)\n"
    )

    def runner(argv, stdin_text, **kwargs):
        return claude_cli._real_runner([sys.executable, "-c", script], stdin_text, **kwargs)

    with pytest.raises(claude_cli.ClaudeUnavailableError) as info:
        list(claude_cli.run("x", runner=runner, flags=frozenset()))
    assert "OAuth session expired" in str(info.value).splitlines()[0]
    assert "claude auth login" in str(info.value)


# --- B3: is_error result raises, exit code 0 or not --------------------------

def test_is_error_result_with_exit_zero_raises_not_saved_as_answer():
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
        _text_line("partial "),
        json.dumps({"type": "result", "subtype": "success", "is_error": True,
                    "result": "API Error: Claude usage limit reached"}),
    ]
    with pytest.raises(claude_cli.ClaudeUnavailableError) as info:
        _deltas(lines)
    assert "usage limit" in str(info.value)
    assert "signed out" not in str(info.value).lower()


def test_error_subtype_result_raises():
    lines = [json.dumps({"type": "result", "subtype": "error_during_execution",
                         "is_error": False})]
    with pytest.raises(claude_cli.ClaudeUnavailableError):
        _deltas(lines)


def test_auth_error_result_raises_with_sign_in_guidance():
    lines = [json.dumps({"type": "result", "subtype": "success", "is_error": True,
                         "result": "Failed to authenticate: OAuth token has expired"})]
    with pytest.raises(claude_cli.ClaudeUnavailableError) as info:
        _deltas(lines)
    assert "claude auth login" in str(info.value)


def test_run_raises_on_is_error_result_through_the_public_api():
    def runner(argv, stdin_text, **kwargs):
        yield json.dumps({"type": "result", "is_error": True, "result": "Prompt is too long"})

    with pytest.raises(claude_cli.ClaudeUnavailableError, match="Prompt is too long"):
        "".join(claude_cli.run("x", runner=runner))


def test_parser_stops_at_result():
    lines = [_text_line("a"), json.dumps({"type": "result", "subtype": "success",
                                          "result": "a"}), _text_line("LATE")]
    assert _deltas(lines) == ["a"]


def test_result_text_is_a_last_resort_fallback_only():
    only_result = [json.dumps({"type": "result", "subtype": "success", "result": "whole"})]
    assert _deltas(only_result) == ["whole"]
    with_text = [_text_line("x"), json.dumps({"type": "result", "subtype": "success",
                                               "result": "x"})]
    assert _deltas(with_text) == ["x"]  # never double-counted


# --- B3: odd shapes never crash ----------------------------------------------

ODD_LINES = [
    "null", "[]", "[1,2]", "\"str\"", "42", "true", "{", "}{", "{\"type\":",
    json.dumps({"type": None}),
    json.dumps({"type": 7}),
    json.dumps({"type": ["stream_event"]}),
    json.dumps({"type": "stream_event"}),
    json.dumps({"type": "stream_event", "event": None}),
    json.dumps({"type": "stream_event", "event": "content_block_delta"}),
    json.dumps({"type": "stream_event", "event": [1]}),
    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta"}}),
    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                  "delta": None}}),
    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                  "delta": "text"}}),
    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                  "delta": {"type": "text_delta",
                                                            "text": None}}}),
    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                  "delta": {"type": "text_delta",
                                                            "text": 12}}}),
    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                  "delta": {"type": "text_delta",
                                                            "text": ["a"]}}}),
    json.dumps({"type": "assistant"}),
    json.dumps({"type": "assistant", "message": None}),
    json.dumps({"type": "assistant", "message": "hi"}),
    json.dumps({"type": "assistant", "message": {"content": None}}),
    json.dumps({"type": "assistant", "message": {"content": "text"}}),
    json.dumps({"type": "assistant", "message": {"content": {"type": "text"}}}),
    json.dumps({"type": "assistant", "message": {"content": [None, 1, "x",
                                                             {"type": "text", "text": None},
                                                             {"type": "text", "text": 5}]}}),
    json.dumps({"type": "system", "session_id": {"id": 1}}),
    json.dumps({"type": "result", "session_id": 99, "result": None}),
]


@pytest.mark.parametrize("line", ODD_LINES)
def test_odd_event_shapes_are_skipped_not_fatal(line):
    # A real stream always ends with its `result` event (SP5 fix B3).
    lines = [line, _text_line("ok"), json.dumps({"type": "result", "result": "ok"})]
    run = claude_cli.ClaudeRun()
    out = list(claude_cli._iter_text_deltas(lines, run))
    assert out == ["ok"] or out == []  # a result line ends the stream early
    assert run.session_id is None


def _random_value(rng, depth=0):
    choices = ["null", "bool", "int", "str", "list", "dict"] if depth < 3 else [
        "null", "bool", "int", "str"]
    kind = rng.choice(choices)
    if kind == "null":
        return None
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "int":
        return rng.randint(-5, 5)
    if kind == "str":
        return rng.choice(["", "text", "text_delta", "content_block_delta", "stream_event",
                           "assistant", "result", "system", "success", "error_x"])
    if kind == "list":
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    keys = ["type", "event", "delta", "text", "message", "content", "result",
            "is_error", "subtype", "session_id"]
    return {rng.choice(keys): _random_value(rng, depth + 1) for _ in range(rng.randint(0, 5))}


def test_parser_fuzz_never_raises_anything_but_a_claude_error():
    rng = random.Random(20260926)
    for _ in range(400):
        lines = []
        for _ in range(rng.randint(1, 8)):
            value = _random_value(rng)
            if isinstance(value, dict) and rng.random() < 0.5:
                value["type"] = rng.choice(["stream_event", "assistant", "result", "system"])
            lines.append(json.dumps(value))
        try:
            out = _deltas(lines)
        except claude_cli.ClaudeUnavailableError:
            continue
        assert all(isinstance(chunk, str) and chunk for chunk in out)
