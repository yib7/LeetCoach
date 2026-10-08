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
        - a follow-up (SP8 / D6)  -> a short canned answer. With
          ``--resume <id>`` it answers "in" that session; without, it is the
          app's fresh fallback call and says it read the fenced STUDY NOTE;
        - a study run             -> a Markdown study doc. For Answer/Guided in
          Python it contains a runnable ```python solution``` block that reads
          stdin, so the app's sandbox verifies it against the pasted samples
          (the Two Sum sample PASSes). A Code Review ("Mode: Code Review")
          critiques the fenced ATTEMPT in the review sections, quoting one
          line of it and never writing a solution.

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
  FAKE_NOWALK Guided/Learning doc without the H3 that follows Hint 4
In a Quick Ask question: FAKE_SLOW sleeps 75 s (tests the 60 s client timeout
and Cancel), FAKE_FAIL returns an error result.
In a follow-up question: FAKE_NORESUME makes ``--resume`` fail like a missing
session ("No conversation found with session ID", exit 1, no output) so the
app falls back; FAKE_SLOW 4x slower deltas (try Stop); FAKE_FAIL error result
mid-stream.

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
  -r, --resume [value]             Resume a conversation by session ID
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


def _attempt_text(prompt: str) -> str:
    """The learner's code in a Code Review prompt (the ATTEMPT fence)."""
    m = re.search(
        r"^--- BEGIN ATTEMPT[^\n]*? (\w+) ---\n(.*?)\n--- END ATTEMPT \1 ---$",
        prompt, re.S | re.M,
    )
    return m.group(2) if m else ""


def _question_text(prompt: str) -> str:
    # prompts.build_quick_ask ends with "Question: <question>".
    m = re.search(r"(?:^|\n)Question: (.*)\Z", prompt.rstrip(), re.S)
    if m:
        return m.group(1).strip()
    return prompt.strip().splitlines()[-1] if prompt.strip() else ""


def _followup_question(prompt: str) -> str:
    """The learner's question in a follow-up prompt (the QUESTION fence)."""
    m = re.search(
        r"^--- BEGIN QUESTION[^\n]*? (\w+) ---\n(.*?)\n--- END QUESTION \1 ---$",
        prompt, re.S | re.M,
    )
    return m.group(2) if m else ""


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
import json
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
    print(json.dumps(answer, separators=(",", ":")))
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


# The D2 doc contract's sections (prompts.DOC_SECTIONS). The fake reads the
# section list the prompt asks for (lines "  ## <title> - <guide>"), so its
# docs follow the contract as it changes; this is only the fallback.
DEFAULT_SECTIONS = (
    "Problem in brief", "Constraints → target complexity", "How to recognize this pattern",
    "Key insight", "Approach", "Solution", "Complexity", "Edge cases", "Common mistakes",
    "Related problems", "Flashcards",
)
HINTS = (
    "Each element needs exactly one partner, and the target fixes that partner's value.",
    "For `x`, the partner is `target - x`. Checking every other element is slow - "
    "find a structure that tells you instantly whether a value was already seen.",
    "Remember every value you have passed, together with its index, in a hash map.",
    "Walk once: if `target - x` is already in the map, return both indices; "
    "otherwise store `x` and keep going.",
)


# A generic map-membership idiom per language for Learning docs - an
# illustration of one operation, never the problem's solution (O4).
IDIOMS = {
    "python": 'ages = {"ana": 31}\nprint("ana" in ages)  # True',
    "cpp": 'std::unordered_map<std::string, int> ages{{"ana", 31}};\n'
           'bool known = ages.count("ana") > 0;  // true',
    "java": 'Map<String, Integer> ages = new HashMap<>();\nages.put("ana", 31);\n'
            'boolean known = ages.containsKey("ana");  // true',
}


def _sections(prompt: str) -> list[str]:
    found = re.findall(r"^  ## (.+?) - ", prompt, re.M)
    return found or list(DEFAULT_SECTIONS)


def _header(problem: str) -> tuple[str, str]:
    """``(line 1, difficulty)`` from the paste: ``# <n>. <Title>``."""
    title = _title(problem)
    m = re.match(r"^#*\s*(\d{1,5})[.):]\s+(.+)$", title)
    head = f"# {m.group(1)}. {m.group(2).strip()}" if m else f"# {title}"
    d = re.search(r"^\s*(?:difficulty\s*:\s*)?(easy|medium|hard)\s*$", problem, re.I | re.M)
    return head, (d.group(1).capitalize() if d else "Easy")


def _review_body(key: str, *, attempt: str) -> str | None:
    """SP7 / D5: a Code Review critiques the learner's attempt - it quotes at
    most a line and never contains a full solution."""
    lines = [ln for ln in attempt.splitlines() if ln.strip()]
    loops = [ln.strip() for ln in lines if re.match(r"\s*for\b", ln)]
    nested = len(loops) >= 2
    quote = loops[-1] if loops else (lines[0].strip() if lines else "")
    if key == "verdict":
        return ("Almost there: the idea is right, but the double loop is O(n^2) and can pair "
                "an element with itself.\n" if nested else
                "Looks correct on the samples; a few edge cases deserve a second look.\n")
    if key == "bugs":
        if not quote:
            return "- No code to review.\n"
        return (f"- `{quote}` - the inner loop also visits `i` itself, so a value equal to "
                "half the target pairs with itself. Start it at `i + 1`.\n")
    if key == "complexity":
        return ("Your attempt: time O(n^2), space O(1). Target: time O(n), space O(n) with a "
                "hash map of complements.\n" if nested else
                "Your attempt: time O(n), space O(n) - already optimal.\n")
    if key == "suggested":
        return ("1. Start the inner index at `i + 1`.\n"
                "2. Then replace the inner scan with a lookup of `target - x` in a dict of "
                "values already seen.\n")
    if key == "readability":
        return "- Name the complement (`need = target - x`) so the intent reads at a glance.\n"
    if key == "flashcards":
        return ("- Q: Why must the second index start after the first? - A: so an element "
                "never pairs with itself.\n"
                "- Q: What turns the O(n^2) pair scan into O(n)? - A: a hash map from value "
                "to index, checked before inserting.\n")
    return None


def _section_body(name: str, *, mode: str, fence: str, tier: str, problem: str,
                  attempt: str = "") -> str:
    key = name.split()[0].lower()
    if mode == "review":
        body = _review_body(key, attempt=attempt)
        if body is not None:
            return body
    hints = "".join(f"### Hint {i}\n\n{text}\n\n" for i, text in enumerate(HINTS, 1))
    if key == "problem":
        return (
            "We get an array and a target and return the indices of the two "
            "values that add up to the target. Exactly one answer exists.\n\n"
            "**Input:** `nums = [2,7,11,15], target = 9`\n\n"
            "**Output:** `[0,1]`\n"
        )
    if key == "constraints":
        return (
            "`2 <= nums.length <= 10^4`, so an O(n^2) scan of every pair is "
            "borderline; aim for O(n) time.\n"
        )
    if key == "how":
        return (
            "The task asks for a pair with a fixed sum, and each lookup only "
            "checks whether the complement exists - a classic hash map signal.\n"
        )
    if key == "key":
        if mode == "learning":
            # SP6 fix O4: Learning never shows solution-shaped code.
            return (
                "Each value has exactly one partner, and the target fixes it. "
                "A structure with constant-time membership turns the search "
                "for that partner into a single lookup.\n"
            )
        return (
            "For each value `x` the partner is `target - x`. Keep a map from "
            "value to index of everything seen so far.\n\n"
            "A quick sketch of the loop (pseudo-code, untagged on purpose):\n\n"
            "```\nfor i, x in nums:\n    if target - x in seen: return [seen[target - x], i]\n"
            "    seen[x] = i\n```\n"
        )
    if key == "approach":
        # SP6 fix I1: a fixed H3 follows the hint ladder; FAKE_NOWALK leaves it
        # out (the app must still end Hint 4 after its own paragraph).
        no_walk = "FAKE_NOWALK" in problem
        if mode == "learning":
            return hints + ("" if no_walk else "### Techniques\n\n") + (
                "**Constant-time membership.** A hash map tells whether a value "
                "was already seen in O(1) on average:\n\n"
                f"```{fence}\n{IDIOMS.get(fence, IDIOMS['python'])}\n```\n\n"
                "Inserting and looking up are both O(1) on average; ordering is "
                "not preserved.\n"
            )
        if mode == "guided":
            return hints + ("" if no_walk else "### Walkthrough\n\n") + (
                "**Brute force:** try every pair `(i, j)` - O(n^2) time, O(1) "
                "space. With 10^4 elements that is 10^8 checks, too slow.\n\n"
                "**Optimal:** one pass with a hash map of complements - O(n) "
                "time, O(n) space.\n"
            )
        return (
            "1. Start from the brute force pair scan.\n"
            "2. Notice each inner scan only asks whether `target - x` exists.\n"
            "3. Replace it with a hash map lookup: one pass, O(n).\n"
        )
    if key == "solution":
        return (
            f"Tier: {tier}.\n\n"
            f"```{fence} solution\n{_solution(fence, problem)}```\n"
        )
    if key == "complexity":
        text = "Complexity: time O(n), space O(n)\n"
        if mode == "answer":
            text += (
                "\n| Tier | Idea | Time | Space |\n|---|---|---|---|\n"
                "| basic | try every pair | O(n^2) | O(1) |\n"
                "| normal | hash map, one pass | O(n) | O(n) |\n"
                "| optimal | same as normal - one pass is optimal | O(n) | O(n) |\n"
            )
        return text
    if key == "edge":
        return "- Duplicates such as `[3,3]` with target 6.\n- Negative numbers.\n"
    if key == "common":
        return "- Inserting before checking, which pairs an element with itself.\n"
    if key == "related":
        return (
            "- 15. 3Sum - the same complement idea, one level deeper.\n"
            "- 167. Two Sum II - sorted input, so two pointers work.\n\n"
            "See <https://leetcode.com/problems/two-sum/?a=1&b=2> for the original "
            "statement (autolink with `&`).\n"
        )
    if key == "flashcards":
        return (
            "- Q: What does the map store? - A: value -> index of every element seen.\n"
            "- Q: Why check before inserting? - A: so an element never pairs with itself.\n"
        )
    return "(fake_claude has no canned text for this section.)\n"


def study_doc(prompt: str) -> str:
    """A Markdown study doc in the D2 contract shape (header, Pattern /
    Difficulty line, the fixed H2 sections the prompt lists; Guided and
    Learning get a ### Hint 1..4 ladder; exactly one solution block, never in
    Learning)."""
    problem = _problem_text(prompt)
    lang = (re.search(r"language key: (\w+)", prompt) or [None, "python"])[1]
    fence = {"python": "python", "cpp": "cpp", "java": "java"}.get(lang, "python")
    tier_m = re.search(r"at the \*\*(\w+)\*\* tier", prompt)
    tier = tier_m.group(1) if tier_m else "normal"
    mode = ("learning" if "Mode: Learning" in prompt
            else "guided" if "Mode: Guided" in prompt
            else "review" if "Mode: Code Review" in prompt else "answer")
    attempt = _attempt_text(prompt) if mode == "review" else ""
    head, difficulty = _header(problem)
    parts = [
        f"{head}\nPattern: Arrays & Hashing · Difficulty: {difficulty}\n\n",
        "> Generated by **fake_claude** for local testing - this is canned text, "
        "not real Claude output.\n\n",
    ]
    for name in _sections(prompt):
        if mode == "learning" and name in ("Solution", "Complexity"):
            continue
        body = _section_body(name, mode=mode, fence=fence, tier=tier, problem=problem,
                             attempt=attempt)
        parts.append(f"## {name}\n\n{body}\n")
    return "".join(parts).rstrip("\n") + "\n"


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


def followup_answer(question: str, *, resumed: bool) -> str:
    """A short canned follow-up answer (no H1/H2, as the prompt asks)."""
    q = " ".join(question.split())[:160]
    where = ("I still have the study note from this session" if resumed
             else "I read the saved study note you sent (there was no session to resume)")
    return (
        f"*(fake_claude canned follow-up)* {where}, so here is the short version.\n\n"
        "- A hash map gives O(1) average lookups, so each element is checked once.\n"
        "- Store each value's index **after** checking for its complement, so an "
        "element is never paired with itself.\n\n"
        "```python\nseen = {}\nfor i, x in enumerate(nums):\n"
        "    if target - x in seen:\n        break\n    seen[x] = i\n```\n\n"
        f"You asked: {q}"
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
           tail: str = "result", session: str | None = None) -> int:
    session = session or str(uuid.uuid4())
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
    # The app writes stdin in text mode, which turns LF into CRLF on Windows.
    raw = sys.stdin.buffer.read().decode("utf-8", "replace").replace("\r\n", "\n")
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

    if "--resume" in argv or "BEGIN QUESTION" in prompt:
        return run_followup(argv, prompt, model, delay)

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


def run_followup(argv: list[str], prompt: str, model: str, delay: float) -> int:
    """SP8 / D6: a follow-up, resumed (``--resume <id>``) or the fallback."""
    question = _followup_question(prompt)
    resume = _arg_after(argv, "--resume") if "--resume" in argv else None
    if resume is not None and "FAKE_NORESUME" in question:
        # Mirrors the real CLI for an unknown session: stderr, exit 1, no stdout.
        sys.stderr.write(f"No conversation found with session ID: {resume}\n")
        return 1
    text = followup_answer(question, resumed=resume is not None)
    if "FAKE_SLOW" in question:
        delay *= 4
    if "FAKE_FAIL" in question:
        return stream(text, model, delay=delay, stop_after=0.4, tail="error", session=resume)
    return stream(text, model, delay=delay, session=resume)


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
