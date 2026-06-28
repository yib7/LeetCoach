"""Tests for `parsing.extract_code` — the markdown -> primary code-block helper.

SP5's sandbox imports the same function to recover runnable code, so its
behaviour is pinned here: prefer the fence matching the requested language,
fall back to the first fence of any kind, and never raise on prose-only text.
"""
from __future__ import annotations

import parsing


def test_extracts_matching_language_fence():
    md = (
        "Here is the solution.\n\n"
        "```python\n"
        "def two_sum(nums, target):\n"
        "    return []\n"
        "```\n\n"
        "Complexity: time O(n), space O(n)."
    )
    code = parsing.extract_code(md, "python")
    assert "def two_sum(nums, target):" in code
    assert "return []" in code
    # surrounding prose is not part of the code
    assert "Complexity" not in code
    assert "Here is the solution" not in code


def test_py_alias_matches_python():
    md = "```py\nprint('hi')\n```"
    assert parsing.extract_code(md, "python") == "print('hi')"


def test_cpp_aliases():
    md = "```c++\nint main(){return 0;}\n```"
    assert parsing.extract_code(md, "cpp") == "int main(){return 0;}"


def test_prefers_requested_language_over_earlier_block():
    md = (
        "Example input:\n"
        "```text\nsome io\n```\n"
        "Solution:\n"
        "```python\nx = 1\n```\n"
    )
    # the python block is wanted even though a ```text block comes first
    assert parsing.extract_code(md, "python") == "x = 1"


def test_falls_back_to_first_fence_when_no_language_match():
    md = "```\njust code no tag\n```"
    assert parsing.extract_code(md, "python") == "just code no tag"


def test_returns_empty_when_no_fence():
    md = "Pure prose, no code at all."
    assert parsing.extract_code(md, "python") == ""


def test_empty_input():
    assert parsing.extract_code("", "python") == ""
    assert parsing.extract_code(None, "python") == ""  # type: ignore[arg-type]
