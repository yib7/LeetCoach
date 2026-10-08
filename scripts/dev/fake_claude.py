#!/usr/bin/env python3
"""A fake `claude` CLI for local UI work and browser verification.

THIS NEVER CALLS REAL CLAUDE. It makes no network requests, reads no
credentials and costs nothing: every reply is canned text generated right
here. It only imitates the parts of the `claude` command-line interface that
LeetCoach drives, so the app can be exercised end to end without a
subscription, a sign-in or any usage:

* ``fake_claude --help``          - lists the long options the app probes for.
* ``fake_claude auth status``     - ``{"loggedIn": true, ...}`` (signed in).
* ``fake_claude -p ... --output-format stream-json ...``
      reads the prompt on stdin and streams realistic stream-json: a
      ``system/init`` event (concrete model id + session id), ``stream_event``
      text deltas, an ``assistant`` message and a terminal ``result`` event.
      It recognises the three kinds of call LeetCoach makes:
        - the problem classifier  -> one compact JSON object;
        - Quick Ask               -> a short Markdown answer;
        - a study run             -> a Markdown study doc. For Answer/Guided in
          Python it contains a runnable ```python solution``` block that reads
          stdin, so the app's sandbox verifies it against the pasted samples
          (the Two Sum sample PASSes).

Knobs (environment):
  FAKE_CLAUDE_DELAY   seconds between text deltas (default 0.15).
  FAKE_CLAUDE_FAIL    "start"   - exit 1 at once with a stderr message;
                      "auth"    - auth status says signed out and runs fail
                                  with an authentication error result;
                      any other non-empty value - a study run streams part of
                      its doc, then ends with an error result (exit 1).

In-band markers (put them in the pasted problem text - no restart needed):
  FAKE_SLOW   4x slower deltas          FAKE_FAIL   error result mid-stream
  FAKE_WRONG  solution prints a wrong answer (verdict FAIL)
  FAKE_CRASH  solution raises (verdict ERROR)
  FAKE_HANG   streams half the doc, then hangs until killed (try Stop)
  FAKE_CUT    streams half the doc, then exits 0 with no result event
In a Quick Ask question: FAKE_SLOW sleeps 75 s (tests the 60 s client timeout
and Cancel), FAKE_FAIL returns an error result.

Launched through ``fake_claude.cmd`` (Windows needs an executable for
``LEETCOACH_CLAUDE_BIN``); see ``scripts/dev/run_fake.py``.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid

MODEL_IDS = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}

HELP_TEXT = """\
Usage: claude [options] [command] [prompt]

Claude Code (FAKE - scripts/dev/fake_claude.py; never calls real Claude)

Options:
  -p, --print                      Print response and exit
  --output-format <format>         Output format: text, json, stream-json
  --include-partial-messages       Include partial message chunks
  --verbose                        Verbose output
  --model <model>                  Model for the session
  --system-prompt <prompt>         System prompt to use for the session
  --tools <tools...>               Built-in tools to enable ("" disables all)
  --strict-mcp-config              Only use MCP servers from --mcp-config
  --safe-mode                      Disable hooks, plugins and customizations
  --no-session-persistence         Do not save the session to disk
  -h, --help                       Display help for command

Commands:
  auth                             Manage authentication
"""


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

def _emit(obj: dict) -> None:
    # ensure_ascii: the stream is pure ASCII, so no console code page can
    # mangle it on its way through the cmd.exe shim.
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _delay() -> float:
    try:
        return max(0.0, float(os.environ.get("FAKE_CLAUDE_DELAY", "0.15")))
    except ValueError:
        return 0.15


def _chunks(text: str, words: int = 5) -> list[str]:
    """Split ``text`` into small deltas of a few words (whitespace kept)."""
    parts = re.findall(r"\S+\s*|\s+", text)
    return ["".join(parts[i:i + words]) for i in range(0, len(parts), words)]


def _arg_after(argv: list[str], flag: str) -> str:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return ""


def _model_id(argv: list[str]) -> str:
    alias = _arg_after(argv, "--model").strip() or "opus"
    return MODEL_IDS.get(alias.lower(), alias)


def _problem_text(prompt: str) -> str:
    m = re.search(
        r"^--- BEGIN PROBLEM[^\n]*? (\w+) ---\n(.*?)\n--- END PROBLEM \1 ---$",
        prompt, re.S | re.M,
    )
    return m.group(2) if m else ""


def _question_text(prompt: str) -> str:
    # prompts.build_quick_ask ends with "Question: <question>".
    m = re.search(r"(?:^|\n)Question: (.*)\Z", prompt.rstrip(), re.S)
    if m:
        return m.group(1).strip()
    return prompt.strip().splitlines()[-1] if prompt.strip() else ""


def _title(problem: str) -> str:
    for line in problem.splitlines():
        if line.strip():
            return line.strip()[:80]
    return "Untitled problem"


# --------------------------------------------------------------------------
# canned content
# --------------------------------------------------------------------------

PY_SOLUTION = '''\
import ast
import re
import sys


class Solution:
    def twoSum(self, nums, target):
        seen = {}
        for i, x in enumerate(nums):
            if target - x in seen:
                return [seen[target - x], i]
            seen[x] = i
        return []


def _parse(text):
    """`nums = [2,7,11,15], target = 9` -> {"nums": [...], "target": 9}."""
    args = {}
    for name, value in re.findall(r"(\\w+)\\s*=\\s*(\\[[^\\]]*\\]|-?\\d+)", text):
        args[name] = ast.literal_eval(value)
    return args


if __name__ == "__main__":
    args = _parse(sys.stdin.read())
    nums = args.get("nums", [])
    target = args.get("target", 0)
    answer = Solution().twoSum(nums, target)__WRONG____CRASH__
    print("[" + ",".join(str(v) for v in answer) + "]")
'''

CPP_SOLUTION = '''\
#include <unordered_map>
#include <vector>
using namespace std;

class Solution {
public:
    vector<int> twoSum(vector<int>& nums, int target) {
        unordered_map<int, int> seen;
        for (int i = 0; i < (int)nums.size(); ++i) {
            auto it = seen.find(target - nums[i]);
            if (it != seen.end()) return {it->second, i};
            seen[nums[i]] = i;
        }
        return {};
    }
};
'''

JAVA_SOLUTION = '''\
import java.util.HashMap;
import java.util.Map;

class Solution {
    public int[] twoSum(int[] nums, int target) {
        Map<Integer, Integer> seen = new HashMap<>();
        for (int i = 0; i < nums.length; i++) {
            Integer j = seen.get(target - nums[i]);
            if (j != null) return new int[] {j, i};
            seen.put(nums[i], i);
        }
        return new int[0];
    }
}
'''


def _solution(language: str, problem: str) -> str:
    if language == "cpp":
        return CPP_SOLUTION
    if language == "java":
        return JAVA_SOLUTION
    code = PY_SOLUTION
    code = code.replace(
        "__WRONG__", "\n    answer = answer[::-1]  # FAKE_WRONG" if "FAKE_WRONG" in problem else ""
    )
    code = code.replace(
        "__CRASH__",
        '\n    raise RuntimeError("FAKE_CRASH")' if "FAKE_CRASH" in problem else "",
    )
    return code


def study_doc(prompt: str) -> str:
    problem = _problem_text(prompt)
    title = _title(problem)
    lang = (re.search(r"language key: (\w+)", prompt) or [None, "python"])[1]
    fence = {"python": "python", "cpp": "cpp", "java": "java"}.get(lang, "python")
    tier_m = re.search(r"at the \*\*(\w+)\*\* tier", prompt)
    tier = tier_m.group(1) if tier_m else "normal"
    learning = "Mode: Learning" in prompt
    guided = "Mode: Guided" in prompt

    parts = [f"# {title}\n\n"]
    parts.append(
        "> Generated by **fake_claude** for local testing - this is canned text, "
        "not real Claude output.\n\n"
    )
    if guided:
        parts.append(
            "## 1. Restate the problem\n\n"
            "We get an array and a target and must return the indices of the two "
            "values that add up to the target. Exactly one answer exists.\n\n"
        )
    parts.append(
        "## Key idea: a hash map of complements\n\n"
        "Walk the array once. For each value `x`, the partner we need is "
        "`target - x`. Keep a map from value to index of everything seen so far; "
        "if the partner is already there, we are done.\n\n"
        "- **Hash map lookups** are O(1) on average.\n"
        "- One pass means each element is touched once.\n"
        "- Storing *after* the check avoids pairing an element with itself.\n\n"
        "A quick sketch of the loop (pseudo-code, untagged on purpose):\n\n"
        "```\nfor i, x in nums:\n    if target - x in seen: return [seen[target - x], i]\n"
        "    seen[x] = i\n```\n\n"
    )
    if learning:
        parts.append(
            "## Using a dictionary idiomatically\n\n"
            "```python\nseen = {}\nseen[7] = 1\nprint(9 - 2 in seen)  # True\n```\n\n"
            "## Practice\n\n"
            "1. Why must we check before inserting?\n"
            "2. What changes if the array were sorted? (Hint: two pointers.)\n\n"
            "Try writing the full solution yourself before switching to Answer mode.\n"
        )
        return "".join(parts)

    parts.append(
        "## Worked example\n\n"
        "**Input:** `nums = [2,7,11,15], target = 9`\n\n"
        "**Output:** `[0,1]`\n\n"
        "At i = 0 we store 2. At i = 1 the partner 9 - 7 = 2 is in the map at "
        "index 0, so the answer is [0,1].\n\n"
        f"## Solution ({tier})\n\n"
        f"```{fence} solution\n{_solution(fence, problem)}```\n\n"
        "Complexity: time O(n), space O(n)\n\n"
        "## Trade-offs\n\n"
        "| Tier | Idea | Time | Space |\n|---|---|---|---|\n"
        "| basic | try every pair | O(n^2) | O(1) |\n"
        "| normal | hash map, one pass | O(n) | O(n) |\n"
        "| optimal | same as normal - you cannot beat one pass | O(n) | O(n) |\n\n"
        "See <https://leetcode.com/problems/two-sum/?a=1&b=2> for the original "
        "statement (autolink with `&`).\n"
    )
    return "".join(parts)


CLASSIFY_RULES = [
    (("two sum", "target", "anagram", "duplicate"), "hash_map", ["hash map", "array"]),
    (("parenthes", "bracket", "stack"), "stack", ["stack", "string"]),
    (("linked list", "listnode"), "linked_list", ["linked list", "pointers"]),
    (("tree", "root"), "trees", ["binary tree", "depth-first search"]),
    (("substring", "window"), "sliding_window", ["sliding window", "string"]),
    (("interval", "meeting"), "intervals", ["intervals", "sorting"]),
    (("sorted", "search"), "binary_search", ["binary search", "array"]),
]


def classify(prompt: str) -> str:
    text = _problem_text(prompt).lower()
    for keys, kind, topics in CLASSIFY_RULES:
        if any(k in text for k in keys):
            return json.dumps({"problem_type": kind, "topics": topics})
    return json.dumps({"problem_type": "dynamic_programming", "topics": ["dynamic programming"]})


def quick_answer(prompt: str) -> str:
    q = _question_text(prompt)
    return (
        "*(fake_claude canned answer)* Use `heapq` for a min-heap: "
        "`heapq.heappush(h, x)` adds and `heapq.heappop(h)` removes the smallest.\n\n"
        "```python\nimport heapq\nh = []\nheapq.heappush(h, 3)\n```\n\n"
        f"You asked: {q[:120]}"
    )


# --------------------------------------------------------------------------
# the stream-json conversation
# --------------------------------------------------------------------------

def _error_result(session: str, message: str) -> None:
    _emit({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": message, "session_id": session,
    })


def stream(text: str, model: str, *, delay: float, stop_after: float | None = None,
           tail: str = "result") -> int:
    session = str(uuid.uuid4())
    _emit({
        "type": "system", "subtype": "init", "session_id": session, "model": model,
        "cwd": os.getcwd(), "tools": [], "mcp_servers": [],
    })
    _emit({"type": "stream_event", "session_id": session,
           "event": {"type": "message_start", "message": {"model": model}}})
    _emit({"type": "stream_event", "session_id": session,
           "event": {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text", "text": ""}}})
    chunks = _chunks(text)
    limit = len(chunks) if stop_after is None else max(1, int(len(chunks) * stop_after))
    for piece in chunks[:limit]:
        _emit({"type": "stream_event", "session_id": session,
               "event": {"type": "content_block_delta", "index": 0,
                         "delta": {"type": "text_delta", "text": piece}}})
        if delay:
            time.sleep(delay)
    if tail == "hang":
        time.sleep(3600)  # until the app's Stop / watchdog kills the tree
        return 0
    if tail == "cut":
        return 0
    if tail == "error":
        _error_result(session, "Simulated failure from fake_claude (FAKE_FAIL).")
        return 1
    _emit({"type": "stream_event", "session_id": session,
           "event": {"type": "content_block_stop", "index": 0}})
    _emit({"type": "assistant", "session_id": session,
           "message": {"model": model, "role": "assistant",
                       "content": [{"type": "text", "text": text}]}})
    _emit({"type": "result", "subtype": "success", "is_error": False,
           "result": text, "session_id": session, "duration_ms": 1234,
           "total_cost_usd": 0, "num_turns": 1})
    return 0


def run_print(argv: list[str]) -> int:
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    system = _arg_after(argv, "--system-prompt")
    prompt = (system + "\n\n" + raw) if system else raw
    model = _model_id(argv)
    delay = _delay()
    fail = os.environ.get("FAKE_CLAUDE_FAIL", "").strip().lower()

    if fail == "auth":
        session = str(uuid.uuid4())
        _emit({"type": "system", "subtype": "init", "session_id": session, "model": model})
        _error_result(session, "Failed to authenticate: OAuth session expired (fake_claude)")
        return 1

    if "strict LeetCode problem classifier" in prompt or "Classify the following" in prompt:
        return stream(classify(prompt), model, delay=0)

    if "LeetCoach Quick Ask" in prompt or "quick-reference assistant" in prompt:
        q = _question_text(prompt)
        if "FAKE_SLOW" in q:
            time.sleep(75)
        if "FAKE_FAIL" in q:
            return stream("", model, delay=0, stop_after=0, tail="error")
        return stream(quick_answer(prompt), model, delay=min(delay, 0.05))

    problem = _problem_text(prompt)
    doc = study_doc(prompt)
    if "FAKE_SLOW" in problem:
        delay *= 4
    if fail or "FAKE_FAIL" in problem:
        return stream(doc, model, delay=delay, stop_after=0.35, tail="error")
    if "FAKE_HANG" in problem:
        return stream(doc, model, delay=delay, stop_after=0.5, tail="hang")
    if "FAKE_CUT" in problem:
        return stream(doc, model, delay=delay, stop_after=0.5, tail="cut")
    return stream(doc, model, delay=delay)


def main(argv: list[str]) -> int:
    fail = os.environ.get("FAKE_CLAUDE_FAIL", "").strip().lower()
    if fail == "start":
        sys.stderr.write("fake_claude: simulated startup failure (FAKE_CLAUDE_FAIL=start)\n")
        return 1
    if "--help" in argv or "-h" in argv:
        sys.stdout.write(HELP_TEXT)
        return 0
    if argv[:2] == ["auth", "status"]:
        logged_in = fail != "auth"
        sys.stdout.write(json.dumps({
            "loggedIn": logged_in, "authMethod": "fake" if logged_in else "none",
            "apiProvider": "fake_claude (never calls real Claude)",
        }) + "\n")
        return 0
    if "-p" in argv or "--print" in argv:
        return run_print(argv)
    sys.stderr.write("fake_claude: unsupported invocation (only --help, auth status, -p)\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
