"""Run LeetCoach against the FAKE `claude` CLI, on a seeded scratch library.

    .venv/Scripts/python.exe scripts/dev/run_fake.py            # http://127.0.0.1:5057
    .venv/Scripts/python.exe scripts/dev/run_fake.py --keep     # keep the previous library

NEVER CALLS REAL CLAUDE and never touches your real data:

* ``LEETCOACH_CLAUDE_BIN`` -> ``scripts/dev/fake_claude.cmd`` (canned
  stream-json; see ``fake_claude.py`` for the FAKE_* knobs and markers);
* ``LEETCOACH_OUTPUT_DIR`` -> ``%TEMP%/leetcoach-fake/output``, re-seeded on
  every start (unless ``--keep``) with pass / fail / error / not-verified /
  learning runs spread over several days. SP6: some runs are in the run log
  (``.leetcoach/runs.jsonl`` + problem records with Easy / Medium / Hard, and
  D2-contract Guided / Learning docs with hints), the rest are legacy files
  with no log entry (Stats' per-file fallback, the doc-parsed verdict), incl.
  extra tiers and ``__2`` slots of one problem. SP7: a Code Review doc
  (``reviews/``), a review queue with problems due today / overdue and one
  not due (``REVIEW_STATE``), a seeded note, ``## Flashcards`` in every
  contract doc, and Two Sum's statement keeps its sample for "Test my code".
  SP8 (D6 follow-up): every logged run has a session id the fake resumes
  except the 206 Java run (``NO_SESSION``: logged, but no session -> the
  fallback); unlogged legacy docs such as ``guided/stack/20_valid_parentheses.md``
  fall back too;
* the real ``.env`` is never loaded (``LEETCOACH_NO_DOTENV=1``) and model-picker
  writes go to a scratch ``.env``; the topic index and the claude cwd are
  scratch files too;
* no browser is opened (``LEETCOACH_NO_BROWSER=1``); fixed port 5057 (the
  ``leetcoach-fake`` entry in ``.claude/launch.json``).

If a LeetCoach already answers ``/healthz`` on 5057 the script just prints its
URL and exits: it never re-seeds (wipes) the library of a running instance.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHIM = ROOT / "scripts" / "dev" / ("fake_claude.cmd" if os.name == "nt" else "fake_claude.py")
PORT = 5057
MARKER = ".leetcoach-fake"

DAY = 86400

PY_TWO_SUM = """\
import sys


class Solution:
    def twoSum(self, nums, target):
        seen = {}
        for i, x in enumerate(nums):
            if target - x in seen:
                return [seen[target - x], i]
            seen[x] = i
        return []
"""


def _doc(title: str, body: str, verification: str | None) -> str:
    text = f"# {title}\n\n> Seeded by scripts/dev/run_fake.py (fake data).\n\n{body}\n"
    if verification is not None:
        text += "\n---\n\n**Verification:** " + verification + "\n"
    return text


# (relative path, content, age in days)
SEED = [
    ("answers/hash_map/two_sum__normal.md",
     _doc("Two Sum", "## Solution\n\n```python solution\n" + PY_TWO_SUM + "```\n\n"
          "Complexity: time O(n), space O(n)", "✓ Sample tests PASS (1/1 samples)"), 0),
    ("answers/hash_map/two_sum__normal.py", PY_TWO_SUM, 0),
    ("answers/stack/valid_parentheses__optimal.md",
     _doc("Valid Parentheses", "## Solution\n\nUse a stack of open brackets.",
          "✗ Sample tests FAIL (1/3 samples passed)"), 1),
    ("answers/stack/valid_parentheses__optimal.py",
     "class Solution:\n    def isValid(self, s):\n        return False\n", 1),
    ("answers/linked_list/reverse_linked_list__normal.md",
     _doc("Reverse Linked List", "## Solution\n\n```java solution\nclass Solution {}\n```",
          "⚠ not auto-verified (Java answers are not run)"), 2),
    ("answers/linked_list/reverse_linked_list__normal.java", "class Solution {}\n", 2),
    ("learning/trees_learning/maximum_depth_of_binary_tree.md",
     _doc("Maximum Depth of Binary Tree",
          "## Depth-first search\n\nRecurse into both children and add one.", None), 3),
    ("guided/sliding_window/longest_substring_without_repeating_characters.md",
     _doc("Longest Substring Without Repeating Characters",
          "## 1. Restate\n\nFind the longest window with unique characters.",
          "✗ Sample tests ERROR (the script raised IndexError)"), 5),
    ("answers/binary_search/binary_search__basic.md",
     _doc("Binary Search", "## Solution\n\nHalve the range each step.",
          "✓ Sample tests PASS (2/2 samples)"), 9),
    ("answers/binary_search/binary_search__basic.py",
     "class Solution:\n    def search(self, nums, target):\n        return -1\n", 9),
    ("answers/intervals/merge_intervals__normal.md",
     _doc("Merge Intervals", "## Solution\n\nSort by start, then sweep.",
          "⚠ not auto-verified (C++ answers are not run)"), 9),
    ("answers/intervals/merge_intervals__normal.cpp", "class Solution {};\n", 9),
    # A8: other tiers / slots of the same problem are separate runs (legacy).
    ("answers/hash_map/two_sum__optimal.md",
     _doc("Two Sum", "## Solution\n\nOne pass with a hash map.",
          "✓ Sample tests PASS (1/1 samples)"), 2),
    ("answers/hash_map/two_sum__normal__2.md",
     _doc("Two Sum", "## Solution\n\nA re-run that did not overwrite the first.",
          "✓ Sample tests PASS (1/1 samples)"), 3),
    ("answers/heap/215_kth_largest_element_in_an_array__optimal.md",
     _doc("215. Kth Largest Element in an Array", "## Solution\n\nA size-k min-heap.",
          "✓ Sample tests PASS (2/2 samples)"), 4),
    ("answers/heap/215_kth_largest_element_in_an_array__optimal.py",
     "import heapq\n\n\nclass Solution:\n    def findKthLargest(self, nums, k):\n"
     "        return heapq.nlargest(k, nums)[-1]\n", 4),
    ("answers/two_pointers/42_trapping_rain_water__optimal.md",
     _doc("42. Trapping Rain Water", "## Solution\n\nTwo pointers from both ends.",
          "⚠ not auto-verified (C++ answers are not run)"), 6),
    ("answers/two_pointers/42_trapping_rain_water__optimal.cpp", "class Solution {};\n", 6),
]


def _fake_claude():
    """The sibling fake_claude module (its D2-contract doc generator)."""
    spec = importlib.util.spec_from_file_location(
        "leetcoach_fake_claude", Path(__file__).resolve().with_name("fake_claude.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# SP7 / D5: the attempt the seeded Code Review critiques (the classic
# "inner loop starts at 0" Two Sum bug).
REVIEW_ATTEMPT = (
    "import json, sys\n\n"
    "def two_sum(nums, target):\n"
    "    for i in range(len(nums)):\n"
    "        for j in range(len(nums)):\n"
    "            if nums[i] + nums[j] == target:\n"
    "                return [i, j]\n"
    "    return []\n"
)


def _review_prompt(problem: str, attempt: str) -> str:
    """A Code Review prompt as prompts.build_review writes one (its review
    sections included, so the fake doc follows the review contract)."""
    sys.path.insert(0, str(ROOT))
    import prompts  # imports nothing that reads .env

    return prompts.build_review(problem, attempt, language="python")


def _contract_doc(mode: str, problem: str, verification: str | None) -> str:
    """A D2-contract study doc exactly as the fake CLI writes one (hints in
    Guided / Learning, a hidden-by-default Solution in Guided). A ``review``
    doc ends with the learner's attempt, as the app saves it."""
    if mode == "review":
        text = _fake_claude().study_doc(_review_prompt(problem, REVIEW_ATTEMPT))
        return (text.rstrip("\n") + "\n\n---\n\n## Your attempt\n\n```python\n" +
                REVIEW_ATTEMPT.rstrip() + "\n```\n")
    prompt = (
        f"Mode: {mode.capitalize()}\nlanguage key: python\n"
        f"--- BEGIN PROBLEM 0f0f0f0f0f0f ---\n{problem}\n--- END PROBLEM 0f0f0f0f0f0f ---\n"
    )
    text = _fake_claude().study_doc(prompt)
    if verification is not None:
        text += "\n\n---\n\n**Verification:** " + verification + "\n"
    return text


TWO_SUM_PASTE = (
    "1. Two Sum\nEasy\n\nGiven an array of integers nums and an integer target, return "
    "indices of the two numbers such that they add up to target.\n\n"
    "Example 1:\nInput: nums = [2,7,11,15], target = 9\nOutput: [0,1]"
)

# D2-contract docs that ARE in the run log (relative path, mode, paste, verdict
# line, age in days).
CONTRACT_SEED = [
    ("guided/hash_map/1_two_sum.md", "guided", TWO_SUM_PASTE,
     "✓ Sample tests PASS (1/1 samples)", 0),
    ("learning/hash_map_learning/1_two_sum.md", "learning", TWO_SUM_PASTE, None, 1),
    ("reviews/hash_map/1_two_sum__review.md", "review", TWO_SUM_PASTE, None, 0),
]

# SP6 fix: a legacy (unlogged) Guided doc whose ## Approach has NO heading after
# Hint 4 (the fake's FAKE_NOWALK marker) - Hint 4 must reveal only its own
# paragraph - and whose numbered file name shows as "#20 - Valid Parentheses".
LEGACY_CONTRACT_SEED = [
    ("guided/stack/20_valid_parentheses.md", "guided",
     "20. Valid Parentheses\nEasy\n\nGiven a string s of brackets... FAKE_NOWALK",
     "✓ Sample tests PASS (1/1 samples)", 2),
]

# problem_id -> (number, title, difficulty, pattern, statement)
PROBLEMS = {
    "1-two_sum": (1, "Two Sum", "Easy", "hash_map", TWO_SUM_PASTE),
    "20-valid_parentheses": (20, "Valid Parentheses", "Easy", "stack",
                             "20. Valid Parentheses\nEasy\n\nGiven a string s of brackets..."),
    "206-reverse_linked_list": (206, "Reverse Linked List", "Easy", "linked_list",
                                "206. Reverse Linked List\nEasy\n\nReverse a singly linked list."),
    "215-kth_largest_element_in_an_array": (
        215, "Kth Largest Element in an Array", "Medium", "heap",
        "215. Kth Largest Element in an Array\nMedium\n\nReturn the kth largest element."),
    "42-trapping_rain_water": (42, "Trapping Rain Water", "Hard", "two_pointers",
                               "42. Trapping Rain Water\nHard\n\nCompute trapped water."),
}

# Run-log entries: (age days, problem_id, mode, language, tier, verdict, files)
LOG = [
    (9, "42-trapping_rain_water", "answer", "cpp", "optimal", "not_verified",
     ["answers/two_pointers/42_trapping_rain_water__optimal.md",
      "answers/two_pointers/42_trapping_rain_water__optimal.cpp"]),
    (4, "215-kth_largest_element_in_an_array", "answer", "python", "optimal", "pass",
     ["answers/heap/215_kth_largest_element_in_an_array__optimal.md",
      "answers/heap/215_kth_largest_element_in_an_array__optimal.py"]),
    (2, "206-reverse_linked_list", "answer", "java", "normal", "not_verified",
     ["answers/linked_list/reverse_linked_list__normal.md",
      "answers/linked_list/reverse_linked_list__normal.java"]),
    (1, "20-valid_parentheses", "answer", "python", "optimal", "fail",
     ["answers/stack/valid_parentheses__optimal.md",
      "answers/stack/valid_parentheses__optimal.py"]),
    (1, "1-two_sum", "learning", "python", None, None,
     ["learning/hash_map_learning/1_two_sum.md"]),
    (0, "1-two_sum", "guided", "python", None, "pass", ["guided/hash_map/1_two_sum.md"]),
    (0, "1-two_sum", "review", "python", None, None, ["reviews/hash_map/1_two_sum__review.md"]),
    (0, "1-two_sum", "answer", "python", "normal", "pass",
     ["answers/hash_map/two_sum__normal.md", "answers/hash_map/two_sum__normal.py"]),
]


# SP8 / D6: logged runs WITHOUT a session id (an older run, or a CLI that never
# reported one): a follow-up on their doc takes the fresh fallback call.
NO_SESSION = {"206-reverse_linked_list"}


# SP7 / D4: review state per problem, relative to today: (box, due in N days
# (negative = overdue), [(graded N days ago, grade, from box, to box)], notes).
# 1-two_sum is due today (graded "solo" 3 days ago: box 1 -> 2, +3 days);
# 215 is NOT due (graded "hints" 2 days ago in box 3: due in 5 days). The
# rest keep the first-run default (due the day after their first run), so
# 42 and 206 show as overdue.
REVIEW_STATE = {
    "1-two_sum": (2, 0, [(3, "solo", 1, 2)], ""),
    "215-kth_largest_element_in_an_array": (
        3, 5, [(9, "solo", 1, 2), (2, "hints", 3, 3)],
        "heapq is a MIN-heap: keep the k largest, the root is the answer."),
}


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")


def _review_state(pid: str, now: float) -> tuple[dict, str] | None:
    """The seeded ``review`` block + notes for ``pid`` (None: the default)."""
    if pid not in REVIEW_STATE:
        return None
    box, due_in, grades, notes = REVIEW_STATE[pid]
    today = datetime.fromtimestamp(now).date()
    intervals = (1, 3, 7, 14, 30)
    history = []
    for ago, grade, from_box, to_box in grades:
        day = today - timedelta(days=ago)
        history.append({
            "ts": _iso(now - ago * DAY), "day": day.isoformat(), "grade": grade,
            "from_box": from_box, "box": to_box,
            "due": (day + timedelta(days=intervals[to_box - 1])).isoformat(),
        })
    review = {"box": box, "due": (today + timedelta(days=due_in)).isoformat(),
              "history": history, "last_reviewed": history[-1]["ts"] if history else None}
    return review, notes


def _seed_metadata(output: Path, now: float) -> None:
    """The run log + problem records for the LOG entries (same shapes as
    problem_store writes)."""
    meta = output / ".leetcoach"
    (meta / "problems").mkdir(parents=True, exist_ok=True)
    lines = []
    records: dict[str, dict] = {}
    for age, pid, mode, lang, tier, verdict, files in sorted(LOG, key=lambda e: -e[0]):
        stamp = now - age * DAY - 600
        number, title, difficulty, pattern, statement = PROBLEMS[pid]
        lines.append(json.dumps({
            "ts": _iso(stamp), "problem_id": pid, "mode": mode, "language": lang,
            "tier": tier, "model": "claude-opus-5-5", "verdict": verdict, "files": files,
            "session_id": None if pid in NO_SESSION else f"fake-session-{len(lines) + 1}",
            "duration_s": 42.0,
            "pattern": pattern,
        }, ensure_ascii=False, separators=(",", ":")))
        rec = records.get(pid)
        if rec is None:
            due = (datetime.fromtimestamp(stamp).date() + timedelta(days=1)).isoformat()
            rec = records[pid] = {
                "id": pid, "number": number, "title": title, "difficulty": difficulty,
                "pattern": pattern, "statement": statement, "created": _iso(stamp),
                "updated": _iso(stamp), "notes": "",
                "review": {"box": 1, "due": due, "history": []}, "runs": [],
            }
        rec["updated"] = _iso(stamp)
        rec["runs"] += [f for f in files if f not in rec["runs"]]
    (meta / "runs.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    for pid, rec in records.items():
        state = _review_state(pid, now)
        if state is not None:
            rec["review"], rec["notes"] = state
        (meta / "problems" / f"{pid}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def seed(output: Path) -> None:
    """(Re)create ``output`` with the fake library. Only ever deletes a
    directory this script created (it carries the marker file)."""
    if output.exists():
        if not (output / MARKER).exists():
            raise SystemExit(f"refusing to wipe {output}: not a run_fake scratch library")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    (output / MARKER).write_text("scratch library created by scripts/dev/run_fake.py\n",
                                 encoding="utf-8")
    now = time.time()
    contract = [(rel, _contract_doc(mode, paste, verdict), age)
                for rel, mode, paste, verdict, age in CONTRACT_SEED + LEGACY_CONTRACT_SEED]
    for rel, content, age_days in SEED + contract:
        path = output / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        stamp = now - age_days * DAY - 600
        os.utime(path, (stamp, stamp))
    _seed_metadata(output, now)


def configure(scratch: Path, *, keep: bool = False) -> Path:
    """Point every LeetCoach env knob at the fake CLI and ``scratch``.

    Must run BEFORE ``import app`` (the .env load happens at import time)."""
    output = scratch / "output"
    if not (keep and output.exists()):
        seed(output)
    (scratch / "claude-cwd").mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "LEETCOACH_CLAUDE_BIN": str(SHIM),
        "LEETCOACH_OUTPUT_DIR": str(output),
        "LEETCOACH_TOPIC_INDEX": str(scratch / "topic_index.json"),
        "LEETCOACH_CLAUDE_CWD": str(scratch / "claude-cwd"),
        "LEETCOACH_NO_DOTENV": "1",
        "LEETCOACH_DOTENV_PATH": str(scratch / "fake.env"),
        "LEETCOACH_NO_BROWSER": "1",
        "FAKE_CLAUDE_PYTHON": sys.executable,
    })
    return output


def running_instance(port: int = PORT, *, timeout: float = 1.0, opener=None) -> str | None:
    """The URL of a LeetCoach already answering ``/healthz`` on ``port``, or
    ``None`` (nothing listening, or some other app)."""
    url = f"http://127.0.0.1:{port}/"
    opener = opener or urllib.request.urlopen
    try:
        with opener(url + "healthz", timeout=timeout) as resp:
            data = json.loads(resp.read(4096).decode("utf-8"))
    except (OSError, ValueError):
        return None
    return url if isinstance(data, dict) and data.get("app") == "leetcoach" else None


def main(argv: list[str], *, probe=running_instance) -> int:
    existing = probe(PORT)
    if existing:
        # SP5 fix R6: re-seeding would wipe the library under a live server.
        print(f"LeetCoach is already running at {existing} - not re-seeding; use that one.")
        return 0
    scratch = Path(tempfile.gettempdir()) / "leetcoach-fake"
    output = configure(scratch, keep="--keep" in argv)
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    import app  # noqa: E402 - env must be set first

    app.PORT = PORT
    print("LeetCoach FAKE mode - the claude CLI is scripts/dev/fake_claude (never real Claude).")
    print(f"Scratch library: {output}")
    return app.main()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
