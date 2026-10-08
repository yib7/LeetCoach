"""Run LeetCoach against the FAKE `claude` CLI, on a seeded scratch library.

    .venv/Scripts/python.exe scripts/dev/run_fake.py            # http://127.0.0.1:5057
    .venv/Scripts/python.exe scripts/dev/run_fake.py --keep     # keep the previous library

NEVER CALLS REAL CLAUDE and never touches your real data:

* ``LEETCOACH_CLAUDE_BIN`` -> ``scripts/dev/fake_claude.cmd`` (canned
  stream-json; see ``fake_claude.py`` for the FAKE_* knobs and markers);
* ``LEETCOACH_OUTPUT_DIR`` -> ``%TEMP%/leetcoach-fake/output``, re-seeded on
  every start (unless ``--keep``) with pass / fail / error / not-verified /
  learning runs spread over several days;
* the real ``.env`` is never loaded (``LEETCOACH_NO_DOTENV=1``) and model-picker
  writes go to a scratch ``.env``; the topic index and the claude cwd are
  scratch files too;
* no browser is opened (``LEETCOACH_NO_BROWSER=1``); fixed port 5057 (the
  ``leetcoach-fake`` entry in ``.claude/launch.json``).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
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
]


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
    for rel, content, age_days in SEED:
        path = output / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        stamp = now - age_days * DAY - 600
        os.utime(path, (stamp, stamp))


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


def main(argv: list[str]) -> int:
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
