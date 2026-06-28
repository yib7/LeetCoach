"""Tests for `storage.py` — the study-library writer.

These tests exercise:

* slug correctness (lowercasing, hyphen/underscore preservation, separator
  stripping, collapsing of junk to a safe token),
* the EXACT output paths each mode writes (learning / guided / answer),
* the security invariant: a malicious problem/type name (``../../etc/x``) can
  NEVER escape ``config.output_dir()``.

The output root is redirected to a temp dir via ``LEETCOACH_OUTPUT_DIR`` so the
tests never touch the real ``output/`` tree.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import storage


@pytest.fixture
def out_root(tmp_path, monkeypatch):
    """Point config.output_dir() at an isolated temp dir for each test."""
    root = tmp_path / "lib"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(root))
    return root


# --- slug correctness ----------------------------------------------------

def test_slug_lowercases_and_replaces_spaces():
    assert storage.slug("Two Sum") == "two_sum"


def test_slug_preserves_hyphen_and_underscore():
    # hyphen and underscore are the only allowed punctuation
    assert storage.slug("two-pointers_fast") == "two-pointers_fast"


def test_slug_strips_path_separators():
    # forward AND back slashes must not survive into a filename
    assert "/" not in storage.slug("a/b\\c")
    assert "\\" not in storage.slug("a/b\\c")


def test_slug_strips_dot_dot_traversal():
    s = storage.slug("../../etc/passwd")
    assert ".." not in s
    assert "/" not in s and "\\" not in s


def test_slug_collapses_unsafe_chars():
    # arbitrary punctuation collapses to underscores, no doubling at the edges
    s = storage.slug("Median of Two Sorted Arrays!!!")
    assert s == "median_of_two_sorted_arrays"


def test_slug_never_empty():
    # even all-garbage input yields a usable, safe token
    s = storage.slug("///...\\\\")
    assert s
    assert s == storage.slug(s)  # idempotent on its own output


def test_slug_suffixes_windows_reserved_names():
    # con/nul/com1/... are illegal Windows filenames even with an extension, so
    # slug must not emit them bare — but must stay safe and non-empty.
    for name in ("con", "CON", "nul", "PRN", "aux", "com1", "LPT9"):
        s = storage.slug(name)
        assert s not in storage._WIN_RESERVED
        assert s
    assert storage.slug("CON") == "con_"
    # a name that merely contains a reserved word is untouched
    assert storage.slug("contains") == "contains"


def test_slug_caps_length():
    # a huge input must not produce a name that overflows the OS path limit
    s = storage.slug("word " * 200)
    assert 0 < len(s) <= storage._MAX_SLUG
    assert not s.endswith(("_", "-"))  # trailing separator trimmed after the cut


def test_problem_name_uses_title_line():
    # a full multi-line paste should save under the title, not the whole body
    problem = "Two Sum\n\nGiven an array of integers nums and a target...\n"
    assert storage._problem_name(problem) == "two_sum"


# --- regression: a full pasted problem must not blow past the path limit -----

def test_save_answer_full_problem_paste_stays_short(out_root):
    # Reproduces the real bug: the textarea holds the WHOLE problem, and slugging
    # the full text produced a 200+ char filename that failed to write on Windows.
    full = (
        "Two Sum\n\nGiven an array of integers nums and an integer target, "
        "return indices of the two numbers such that they add up to target. "
    ) * 5  # long, multi-sentence body
    code_path, reasoning_path = storage.save_answer(
        full, "hash_map", tier="normal", language="python",
        code="print('[0,1]')", reasoning="reasoning",
    )
    for path in (code_path, reasoning_path):
        p = Path(path)
        assert p.exists()  # the write actually succeeded
        # the filename stem stays bounded (title-derived + capped)
        assert len(p.name) <= storage._MAX_SLUG + len("__normal.py")
        assert p.name.startswith("two_sum")


# --- exact output paths per mode -----------------------------------------

def test_save_learning_path(out_root):
    path = storage.save_learning("Two Sum", "two_pointers", "learning body")
    p = Path(path)
    assert p == out_root / "learning" / "two_pointers_learning" / "two_sum.md"
    assert p.read_text(encoding="utf-8") == "learning body"


def test_save_guided_path(out_root):
    path = storage.save_guided("Two Sum", "two_pointers", "guided body")
    p = Path(path)
    assert p == out_root / "guided" / "two_pointers" / "two_sum.md"
    assert p.read_text(encoding="utf-8") == "guided body"


def test_save_answer_python_paths(out_root):
    code_path, reasoning_path = storage.save_answer(
        "Two Sum",
        "two_pointers",
        tier="normal",
        language="python",
        code="print('hi')",
        reasoning="# why\nBig-O: O(n)",
    )
    cp = Path(code_path)
    rp = Path(reasoning_path)
    # code file: <problem>__<tier>.<ext> under answers/<type>/
    assert cp == out_root / "answers" / "two_pointers" / "two_sum__normal.py"
    # sibling reasoning markdown
    assert rp == out_root / "answers" / "two_pointers" / "two_sum__normal.md"
    assert cp.read_text(encoding="utf-8") == "print('hi')"
    assert "Big-O" in rp.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "language,ext",
    [("python", "py"), ("cpp", "cpp"), ("java", "java")],
)
def test_save_answer_extension_per_language(out_root, language, ext):
    code_path, _ = storage.save_answer(
        "Add Two", "linked_list", tier="simple", language=language,
        code="x", reasoning="r",
    )
    assert Path(code_path).name == f"add_two__simple.{ext}"


def test_save_answer_creates_parent_dirs(out_root):
    # nothing exists yet; save must create the full tree
    assert not out_root.exists()
    storage.save_answer(
        "Brand New", "fresh_type", tier="complex", language="python",
        code="x", reasoning="r",
    )
    assert (out_root / "answers" / "fresh_type" / "brand_new__complex.py").exists()


# --- security: cannot escape output/ -------------------------------------

def test_malicious_problem_name_cannot_escape(out_root):
    path = storage.save_learning("../../etc/x", "../../sys", "body")
    p = Path(path).resolve()
    root = out_root.resolve()
    # the written file must live strictly inside the output root
    assert root in p.parents
    # and the traversal must not have created anything outside
    assert ".." not in str(p)


def test_malicious_type_in_answer_cannot_escape(out_root):
    code_path, reasoning_path = storage.save_answer(
        "..\\..\\win", "..\\..\\evil", tier="..\\nope", language="python",
        code="x", reasoning="r",
    )
    for path in (code_path, reasoning_path):
        p = Path(path).resolve()
        assert out_root.resolve() in p.parents


def test_absolute_problem_name_cannot_escape(out_root):
    # an absolute-looking name must be neutralised, not honoured
    path = storage.save_guided("/etc/shadow", "C:/Windows/system32", "body")
    p = Path(path).resolve()
    assert out_root.resolve() in p.parents


def test_returned_path_is_within_configured_root(out_root):
    path = storage.save_answer(
        "Two Sum", "two_pointers", tier="normal", language="python",
        code="c", reasoning="r",
    )[0]
    # commonpath raises if path is not under root; this asserts containment
    common = os.path.commonpath([Path(path).resolve(), out_root.resolve()])
    assert common == str(out_root.resolve())
