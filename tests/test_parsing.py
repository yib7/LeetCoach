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


def test_nested_indented_fence_does_not_truncate_code():
    # P2-5: a solution whose docstring embeds a fenced markdown example (the
    # inner ``` is indented) must NOT truncate at that inner fence — the whole
    # function body, including code AFTER the nested example, is extracted.
    md = (
        "Here is the solution.\n\n"
        "```python\n"
        "def solve():\n"
        '    doc = """\n'
        "    Example usage:\n"
        "    ```\n"
        "    solve()\n"
        "    ```\n"
        '    """\n'
        "    return 42\n"
        "```\n\n"
        "Complexity: O(1)."
    )
    code = parsing.extract_code(md, "python")
    assert "def solve():" in code
    assert "return 42" in code          # not truncated at the inner fence
    assert "    ```" in code            # the indented inner fences are preserved
    assert "Complexity" not in code     # prose after the real close is excluded


def test_returns_empty_when_no_fence():
    md = "Pure prose, no code at all."
    assert parsing.extract_code(md, "python") == ""


def test_empty_input():
    assert parsing.extract_code("", "python") == ""
    assert parsing.extract_code(None, "python") == ""  # type: ignore[arg-type]


# --- A2: pick the RIGHT block in a multi-block Guided/Answer doc ---------

def test_multi_block_guided_prefers_tagged_solution_block():
    # A Guided doc teaches with an earlier illustrative snippet, then the real
    # answer is fenced ```python solution — that one must win even though it's
    # not first and the earlier snippet also matches the language.
    md = (
        "Teaching example:\n"
        "```python\n"
        "# just illustrating a hash map lookup\n"
        "d = {}\n"
        "```\n\n"
        "Now the real answer:\n"
        "```python solution\n"
        "def two_sum(nums, target):\n"
        "    return [0, 1]\n"
        "```\n"
    )
    code = parsing.extract_code(md, "python")
    assert "def two_sum" in code
    assert "just illustrating" not in code


def test_multi_block_guided_prefers_last_block_with_main_guard():
    # No block is tagged "solution", but the LAST python block has a runnable
    # __main__ driver (the real answer) while an earlier one is just a snippet.
    # The old bug returned the FIRST language-tagged block (the snippet).
    md = (
        "```python\n"
        "# teaching snippet, not the real solution\n"
        "x = [1, 2, 3]\n"
        "```\n\n"
        "```python\n"
        "def two_sum(nums, target):\n"
        "    return [0, 1]\n\n"
        "if __name__ == \"__main__\":\n"
        "    print(two_sum([2, 7], 9))\n"
        "```\n"
    )
    code = parsing.extract_code(md, "python")
    assert "def two_sum" in code
    assert "__main__" in code
    assert "teaching snippet" not in code


def test_multi_block_no_marker_prefers_last_tagged_block():
    # No "solution" tag and no marker in either block: still prefer the LAST
    # language-tagged block (Guided puts the real answer last).
    md = (
        "```python\nfirst_block = 1\n```\n\n"
        "```python\nsecond_block = 2\n```\n"
    )
    assert parsing.extract_code(md, "python") == "second_block = 2"


def test_indented_list_item_fence_is_extracted_correctly():
    # A fence nested inside a numbered list item is indented; the code itself
    # (and its own internal indentation) must come out dedented and intact.
    md = (
        "1. Here is the approach:\n\n"
        "   ```python\n"
        "   def solve():\n"
        "       return 42\n"
        "   ```\n\n"
        "2. That's it.\n"
    )
    code = parsing.extract_code(md, "python")
    assert code == "def solve():\n    return 42"


def test_legacy_single_untagged_block_still_extracted():
    # Backward compat: an old-style doc with one plain, untagged fence must
    # still degrade to that block (unchanged pre-A2 behaviour).
    md = "```\ndef legacy():\n    return 1\n```\n"
    assert parsing.extract_code(md, "python") == "def legacy():\n    return 1"
