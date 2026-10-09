"""SP6 fix round: problem-store robustness (M1 difficulty source + record
merge, M2 generic first lines, M3 log-first, M4 caches, M8 no-files runs),
the /problems list shape (O1), no tier outside Answer (O2) and the Guided /
Learning approach-section contract (I1). Every Claude call is a fake."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest

import fsutil
import problem_store as ps
import prompts

NOW = datetime(2026, 10, 8, 14, 30).astimezone()


@pytest.fixture
def root(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return out


def _record(root, paste, rel="answers/hash_map/two_sum__normal.md", **kw):
    args = {
        "mode": "answer", "language": "python", "tier": "normal", "model": "m", "verdict": "pass",
        "paths": [str(root / rel)], "session_id": None, "duration_s": 1.0,
        "pattern": "hash_map", "doc": "", "now": NOW, "root": root,
    }
    args.update(kw)
    return ps.record_run(paste, **args)


# --- M1: difficulty source ------------------------------------------------------------

def test_doc_guessed_difficulty_is_marked_and_a_paste_overrides_it(root):
    doc = "# 70. Climbing Stairs\nPattern: Dynamic Programming · Difficulty: Medium\n"
    pid = _record(root, "Climbing Stairs\nYou are climbing...", doc=doc)
    rec = ps.load_problem(pid, root=root)
    assert (rec["difficulty"], rec["difficulty_source"]) == ("Medium", "doc")
    _record(root, "Climbing Stairs\nEasy\nYou are climbing...",
            rel="answers/dp/climbing_stairs__optimal.md")
    rec = ps.load_problem(pid, root=root)
    assert (rec["difficulty"], rec["difficulty_source"]) == ("Easy", "paste")


def test_a_doc_guess_never_overrides_a_pasted_difficulty(root):
    pid = _record(root, "Climbing Stairs\nEasy\nYou are climbing...")
    doc = "# Climbing Stairs\nPattern: x · Difficulty: Hard\n"
    _record(root, "Climbing Stairs\nYou are climbing...", doc=doc,
            rel="answers/dp/climbing_stairs__optimal.md")
    rec = ps.load_problem(pid, root=root)
    assert (rec["difficulty"], rec["difficulty_source"]) == ("Easy", "paste")


# --- M1: Two Sum then 1. Two Sum is ONE record --------------------------------------

def test_a_numbered_paste_merges_the_unnumbered_record(root):
    first = _record(root, "Two Sum\nGiven nums...", rel="answers/hash_map/two_sum__normal.md")
    assert first == "two_sum"
    second = _record(root, "1. Two Sum\nEasy\nGiven nums and target...",
                     rel="answers/hash_map/1_two_sum__optimal.md",
                     now=NOW + timedelta(days=1))
    assert second == "1-two_sum"
    ids = [p["id"] for p in ps.list_problems(root=root)]
    assert ids == ["1-two_sum"]
    rec = ps.load_problem("1-two_sum", root=root)
    assert rec["runs"] == ["answers/hash_map/two_sum__normal.md",
                           "answers/hash_map/1_two_sum__optimal.md"]
    assert rec["aliases"] == ["two_sum"]
    assert rec["created"] == NOW.isoformat(timespec="seconds")
    assert rec["number"] == 1 and rec["difficulty"] == "Easy"
    # the alias still resolves, and the old id's log entries belong to the record
    assert ps.load_problem("two_sum", root=root)["id"] == "1-two_sum"
    assert [e["problem_id"] for e in ps.runs_for("1-two_sum", root=root)] == [
        "two_sum", "1-two_sum"]
    # path lookups of the old run see the merged record's difficulty
    idx = ps.path_index(root=root)
    assert idx["answers/hash_map/two_sum__normal.md"]["difficulty"] == "Easy"


def test_an_unnumbered_paste_joins_the_numbered_record(root):
    _record(root, "1. Two Sum\nEasy\nGiven nums...")
    pid = _record(root, "Two Sum\nGiven nums...", rel="learning/hash_map_learning/two_sum.md",
                  mode="learning", tier=None, verdict=None)
    assert pid == "1-two_sum"
    assert [p["id"] for p in ps.list_problems(root=root)] == ["1-two_sum"]


# --- M2: generic first lines --------------------------------------------------------

@pytest.mark.parametrize("paste,title", [
    ("Description\nTwo Sum\nGiven nums...", "Two Sum"),
    ("Problem:\n\n3Sum\nMedium\n", "3Sum"),
    ("Problem: Valid Parentheses\nEasy", "Valid Parentheses"),
    ("---\nLongest Palindrome\n", "Longest Palindrome"),
    ("## Problem\n42. Trapping Rain Water\nHard", "Trapping Rain Water"),
])
def test_generic_first_lines_are_skipped(paste, title):
    p = ps.parse_problem(paste)
    assert p.title == title
    assert not p.generic


def test_difficulty_and_number_after_a_skipped_line():
    p = ps.parse_problem("Problem\n42. Trapping Rain Water\nHard\n")
    assert (p.number, p.title, p.difficulty) == (42, "Trapping Rain Water", "Hard")


def test_all_generic_pastes_get_distinct_hashed_ids():
    a = ps.parse_problem("Description\n\nGiven an array of ints, return two indices.")
    b = ps.parse_problem("Description\n\nGiven a string, decide if brackets match.")
    # the body line becomes the title - unrelated problems stay apart
    assert ps.problem_id(a) != ps.problem_id(b)
    c = ps.parse_problem("Problem:\n???\nuntitled")
    d = ps.parse_problem("Problem:\n!!!\nuntitled\n")
    assert c.generic and d.generic
    assert ps.problem_id(c).startswith("untitled_")
    assert ps.valid_problem_id(ps.problem_id(c))
    e = ps.parse_problem("Problem:\n???\nuntitled\n\nsomething else entirely")
    assert ps.problem_id(c) != ps.problem_id(e)


def test_generic_records_do_not_collapse(root):
    a = _record(root, "Problem\n???\nnums and target", rel="answers/x/a.md")
    b = _record(root, "Problem\n???\nbrackets", rel="answers/x/b.md")
    assert a != b
    assert len(ps.list_problems(root=root)) == 2


# --- M3: the log line is written even when the record write fails --------------------

def test_log_line_survives_a_failing_record_write(root, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil, "atomic_write_text", boom)
    pid = _record(root, "1. Two Sum\nEasy\n")
    assert pid == "1-two_sum"
    [entry] = ps.read_runs(root=root)
    assert entry["problem_id"] == "1-two_sum"
    assert ps.load_problem("1-two_sum", root=root) is None


# --- M8: a run that saved nothing is not logged ---------------------------------------

def test_no_files_no_log_entry(root):
    assert _record(root, "1. Two Sum\nEasy\n", paths=[]) is None
    assert ps.read_runs(root=root) == []
    assert ps.list_problems(root=root) == []


# --- O2: tier only for Answer -------------------------------------------------------

@pytest.mark.parametrize("mode", ["guided", "learning"])
def test_non_answer_modes_log_no_tier(root, mode):
    _record(root, "1. Two Sum\n", mode=mode, tier="optimal")
    [entry] = ps.read_runs(root=root)
    assert entry["tier"] is None


# --- M4: cached log / record index, invalidated by external changes ------------------

def test_read_runs_is_cached_and_sees_external_appends(root, monkeypatch):
    _record(root, "1. Two Sum\nEasy\n")
    assert len(ps.read_runs(root=root)) == 1
    log = root / ".leetcoach" / "runs.jsonl"
    reads = []
    real = type(log).read_bytes

    def counting(self):
        reads.append(self)
        return real(self)

    monkeypatch.setattr(type(log), "read_bytes", counting)
    ps.read_runs(root=root)
    ps.read_runs(root=root)
    assert reads == []  # unchanged (mtime, size): served from the cache
    with open(log, "ab") as fh:  # an external append (another tool / process)
        fh.write(b'{"problem_id":"x","files":[]}\n')
    assert [e["problem_id"] for e in ps.read_runs(root=root)] == ["1-two_sum", "x"]
    assert len(reads) == 1


def test_record_index_has_no_statements_and_tracks_record_edits(root):
    _record(root, "1. Two Sum\nEasy\nA long statement body...")
    idx = ps.record_index(root=root)
    assert set(idx["1-two_sum"]) <= {"id", "number", "title", "difficulty"}
    path = root / ".leetcoach" / "problems" / "1-two_sum.json"
    rec = json.loads(path.read_text("utf-8"))
    rec["difficulty"] = "Hard"
    path.write_text(json.dumps(rec), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    assert ps.record_index(root=root)["1-two_sum"]["difficulty"] == "Hard"
    assert ps.path_index(root=root)["answers/hash_map/two_sum__normal.md"]["difficulty"] == "Hard"


def test_store_signature_changes_on_append(root):
    before = ps.store_signature(root=root)
    _record(root, "1. Two Sum\nEasy\n")
    after = ps.store_signature(root=root)
    assert before != after


def test_path_index_carries_title_and_number(root):
    _record(root, "1. Two Sum\nEasy\n")
    hit = ps.path_index(root=root)["answers/hash_map/two_sum__normal.md"]
    assert (hit["title"], hit["number"]) == ("Two Sum", 1)


# --- I1: a fixed H3 follows the hint ladder ---------------------------------------------

def test_guided_requires_a_walkthrough_heading_after_the_hints():
    p = prompts.build_guided("1. Two Sum\n", tier="normal", language="python")
    assert "`### Walkthrough`" in p
    assert p.index("### Hint 4") < p.index("`### Walkthrough`")


def test_learning_requires_a_techniques_heading_after_the_hints():
    p = prompts.build_learning("1. Two Sum\n", language="python")
    assert "`### Techniques`" in p
    assert "### Walkthrough" not in p


def test_answer_has_neither_fixed_heading():
    p = prompts.build_answer("1. Two Sum\n", tier="normal", language="python")
    assert "### Walkthrough" not in p and "### Techniques" not in p
