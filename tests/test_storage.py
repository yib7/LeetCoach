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
import threading
from pathlib import Path

import pytest

import config
import storage


@pytest.fixture
def out_root(tmp_path, monkeypatch):
    """Point config.output_dir() at an isolated temp dir for each test."""
    root = tmp_path / "lib"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(root))
    return root


# --- config: default output dir is anchored, not CWD-relative (P2-10) ----

def test_default_output_dir_is_anchored_next_to_config(monkeypatch):
    # Without the env override, output_dir() must be an ABSOLUTE path anchored
    # to the directory containing config.py — launching from another CWD
    # (e.g. `flask run` elsewhere) must not fork the study library.
    monkeypatch.delenv("LEETCOACH_OUTPUT_DIR", raising=False)
    expected = Path(config.__file__).resolve().parent / "output"
    assert config.output_dir() == expected
    assert config.output_dir().is_absolute()


def test_output_dir_env_override_kept_verbatim(monkeypatch):
    # An explicit override — even a relative one — is the user's choice and is
    # passed through untouched.
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", "my_lib")
    assert config.output_dir() == Path("my_lib")


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


# --- collisions: suffix instead of clobber (P2-11) ------------------------

def test_rerun_identical_content_is_idempotent(out_root):
    # Same problem, same body -> same path, ONE file, no __2 duplicate.
    p1 = storage.save_learning("Two Sum", "arrays", "same body")
    p2 = storage.save_learning("Two Sum", "arrays", "same body")
    assert p1 == p2
    folder = out_root / "learning" / "arrays_learning"
    assert [f.name for f in folder.iterdir()] == ["two_sum.md"]


def test_different_content_gets_suffixed_not_clobbered(out_root):
    # A second, different write must NOT overwrite the first note.
    p1 = storage.save_learning("Two Sum", "arrays", "first")
    p2 = storage.save_learning("Two Sum", "arrays", "second")
    p3 = storage.save_learning("Two Sum", "arrays", "third")
    assert Path(p1).name == "two_sum.md"
    assert Path(p2).name == "two_sum__2.md"
    assert Path(p3).name == "two_sum__3.md"
    assert Path(p1).read_text(encoding="utf-8") == "first"
    assert Path(p2).read_text(encoding="utf-8") == "second"
    assert Path(p3).read_text(encoding="utf-8") == "third"


def test_suffixed_slot_is_idempotent_too(out_root):
    # Re-running the SECOND variant lands back on __2, not __3.
    storage.save_guided("Two Sum", "arrays", "first")
    p2 = storage.save_guided("Two Sum", "arrays", "second")
    p2_again = storage.save_guided("Two Sum", "arrays", "second")
    assert p2 == p2_again
    folder = out_root / "guided" / "arrays"
    assert sorted(f.name for f in folder.iterdir()) == ["two_sum.md", "two_sum__2.md"]


def test_answer_rerun_identical_pair_is_idempotent(out_root):
    kwargs = dict(tier="normal", language="python", code="code", reasoning="why")
    first = storage.save_answer("Two Sum", "arrays", **kwargs)
    second = storage.save_answer("Two Sum", "arrays", **kwargs)
    assert first == second
    folder = out_root / "answers" / "arrays"
    assert sorted(f.name for f in folder.iterdir()) == [
        "two_sum__normal.md", "two_sum__normal.py",
    ]


def test_answer_pair_moves_together_when_one_sibling_collides(out_root):
    # Pre-create ONLY the code path with different content: the pair is one
    # logical entry, so BOTH new files must land on the same __2 stem — the
    # code must not go to __2 while the .md stays bare.
    folder = out_root / "answers" / "arrays"
    folder.mkdir(parents=True)
    (folder / "two_sum__normal.py").write_text("old code", encoding="utf-8")
    code_path, reasoning_path = storage.save_answer(
        "Two Sum", "arrays", tier="normal", language="python",
        code="new code", reasoning="why",
    )
    assert Path(code_path).name == "two_sum__normal__2.py"
    assert Path(reasoning_path).name == "two_sum__normal__2.md"
    # original untouched
    assert (folder / "two_sum__normal.py").read_text(encoding="utf-8") == "old code"
    assert Path(code_path).read_text(encoding="utf-8") == "new code"
    assert Path(reasoning_path).read_text(encoding="utf-8") == "why"


# --- concurrency: parallel writes lose no material (P1-2 / P2-8) ----------

def test_concurrent_save_learning_loses_no_bodies(out_root):
    """Flask runs ``threaded=True``. Many concurrent identical-shaped Learning
    saves (same problem/type, distinct bodies) must not lose material via a
    check-then-write race: resolving the collision-free slot and writing to it
    must be one atomic step, so every distinct body survives under some
    ``__N`` suffix.

    Regression guard for P1-2: without a lock, threads can all observe "slot 1
    is free" before any of them writes, so later writes silently clobber
    earlier ones and most bodies never make it to disk.
    """
    n_threads = 24
    bodies = [f"body_{i}" for i in range(n_threads)]
    barrier = threading.Barrier(n_threads)

    def worker(i):
        # Line every thread up at the barrier so they hammer save_learning()
        # together, maximizing the interleave that would trigger the race.
        barrier.wait()
        storage.save_learning("Two Sum", "arrays", bodies[i])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "a save_learning worker hung"

    folder = out_root / "learning" / "arrays_learning"
    on_disk_files = list(folder.iterdir())
    on_disk_bodies = {f.read_text(encoding="utf-8") for f in on_disk_files}
    missing = set(bodies) - on_disk_bodies
    assert not missing, f"lost {len(missing)} bodies under concurrency: {sorted(missing)}"
    # Every distinct body must have landed on its OWN file — if two threads
    # had shared (and clobbered) a slot, this count would be lower than
    # n_threads even though the "missing" check above happened to pass.
    assert len(on_disk_files) == n_threads


def test_concurrent_save_answer_pairs_stay_matched(out_root):
    """Same race as above, but for the code+reasoning PAIR that ``save_answer``
    writes as one logical entry. This proves the lock covers ``_resolve_slot``
    AND both writes together as a single critical section — a lock that only
    wrapped ``_resolve_slot`` and released before writing would still let two
    threads resolve to the same free slot before either had written, which
    would surface here as a missing pair or a code/reasoning mismatch.
    """
    n_threads = 24
    barrier = threading.Barrier(n_threads)

    def worker(i):
        barrier.wait()
        storage.save_answer(
            "Two Sum", "arrays", tier="normal", language="python",
            code=f"code_{i}", reasoning=f"reasoning_{i}",
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "a save_answer worker hung"

    folder = out_root / "answers" / "arrays"
    py_files = sorted(folder.glob("*.py"))
    md_files = sorted(folder.glob("*.md"))
    assert len(py_files) == n_threads, (
        f"expected {n_threads} code files, found {len(py_files)} "
        "(a shared slot means a pair was clobbered)"
    )
    assert len(md_files) == n_threads

    # Every code file's sibling reasoning file must carry the MATCHING index —
    # proving the pair moved to its slot together, not as two independent
    # writes that could land on different slots.
    for py in py_files:
        i = py.read_text(encoding="utf-8").removeprefix("code_")
        sibling_md = py.with_suffix(".md")
        assert sibling_md.exists(), f"{py.name} has no sibling reasoning file"
        assert sibling_md.read_text(encoding="utf-8") == f"reasoning_{i}"
