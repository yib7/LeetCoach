"""SP6 / D1: the problem store (``output/.leetcoach/problems/<id>.json``) and
the append-only run log (``output/.leetcoach/runs.jsonl``)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

import pytest

import problem_store as ps

LC_PASTE = (
    "1. Two Sum\n"
    "Solved\n"
    "Easy\n"
    "Topics\n"
    "Companies\n"
    "\n"
    "Given an array of integers nums and an integer target, return indices.\n"
    "Example 1:\nInput: nums = [2,7,11,15], target = 9\nOutput: [0,1]\n"
)


@pytest.fixture
def root(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return out


# --- parsing the paste ----------------------------------------------------------

def test_parse_leetcode_paste_number_title_difficulty():
    p = ps.parse_problem(LC_PASTE)
    assert (p.number, p.title, p.difficulty) == (1, "Two Sum", "Easy")


@pytest.mark.parametrize("paste,expected", [
    ("Two Sum\n\nGiven nums...", (None, "Two Sum", None)),
    ("  ## 15) 3Sum\nMedium\n", (15, "3Sum", "Medium")),
    ("42. Trapping Rain Water (Hard)\nGiven n...", (42, "Trapping Rain Water", "Hard")),
    ("Valid Parentheses\nDifficulty: medium\n", (None, "Valid Parentheses", "Medium")),
    ("Two Sum\r\nHARD\r\n", (None, "Two Sum", "Hard")),
    ("1 <= nums.length <= 10^4\n", (None, "1 <= nums.length <= 10^4", None)),
])
def test_parse_variants(paste, expected):
    p = ps.parse_problem(paste)
    assert (p.number, p.title, p.difficulty) == expected


def test_difficulty_only_looked_for_near_the_top():
    body = "Two Sum\n" + "line\n" * 30 + "Easy\n"
    assert ps.parse_problem(body).difficulty is None


def test_parse_empty_paste():
    p = ps.parse_problem("   \n\n")
    assert p.number is None and p.difficulty is None
    assert p.title == ""


def test_doc_header_difficulty_and_number():
    doc = "# 1. Two Sum\n\nPattern: Arrays & Hashing · Difficulty: Easy\n\n## Problem in brief\n"
    assert ps.parse_doc_header(doc) == (1, "Easy")
    assert ps.parse_doc_header("# Two Sum\nno header\n") == (None, None)


# --- ids ----------------------------------------------------------------------

def test_problem_id_with_and_without_number():
    assert ps.problem_id(ps.parse_problem(LC_PASTE)) == "1-two_sum"
    assert ps.problem_id(ps.parse_problem("Two Sum\nbody")) == "two_sum"


def test_problem_id_non_ascii_gets_hash_suffix():
    a = ps.problem_id(ps.parse_problem("两数之和\n"))
    b = ps.problem_id(ps.parse_problem("三数之和\n"))
    assert a != b and a.startswith("untitled_")
    assert ps.valid_problem_id(a)


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "A-b", ".hidden", "x" * 200, "a\\b", "-x"])
def test_invalid_ids_rejected(bad):
    assert not ps.valid_problem_id(bad)


# --- run log ------------------------------------------------------------------

def test_append_and_read_runs(root):
    ps.append_run({"problem_id": "a", "mode": "answer"}, root=root)
    ps.append_run({"problem_id": "b", "mode": "learning"}, root=root)
    entries = ps.read_runs(root=root)
    assert [e["problem_id"] for e in entries] == ["a", "b"]
    raw = (root / ".leetcoach" / "runs.jsonl").read_bytes()
    assert raw.count(b"\n") == 2 and b"\r\n" not in raw


def test_reader_skips_torn_and_garbage_lines_and_append_repairs(root):
    log = root / ".leetcoach" / "runs.jsonl"
    log.parent.mkdir(parents=True)
    log.write_bytes(b'{"problem_id": "a"}\nnot json\n[1,2]\n{"problem_id": "b", "mo')
    assert [e["problem_id"] for e in ps.read_runs(root=root)] == ["a"]
    ps.append_run({"problem_id": "c"}, root=root)
    # the torn tail stays a single bad line; the new record is on its own line
    assert [e["problem_id"] for e in ps.read_runs(root=root)] == ["a", "c"]


def test_missing_log_reads_empty(root):
    assert ps.read_runs(root=root) == []


def test_concurrent_appends_never_interleave(root):
    def worker(n):
        for i in range(25):
            ps.append_run({"problem_id": f"p{n}", "i": i, "pad": "x" * 500}, root=root)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    entries = ps.read_runs(root=root)
    assert len(entries) == 150


# --- record_run: problem record + log line ----------------------------------------

NOW = datetime(2026, 10, 8, 14, 30).astimezone()


def _record(root, paste=LC_PASTE, **kw):
    args = dict(
        mode="answer", language="python", tier="normal", model="claude-opus-5-5",
        verdict="pass", paths=[str(root / "answers/hash_map/1_two_sum__normal.md"),
                               str(root / "answers/hash_map/1_two_sum__normal.py")],
        session_id="sess-1", duration_s=12.345, pattern="hash_map", doc="",
        now=NOW, root=root,
    )
    args.update(kw)
    return ps.record_run(paste, **args)


def test_record_run_writes_problem_and_log_line(root):
    pid = _record(root)
    assert pid == "1-two_sum"
    rec = json.loads((root / ".leetcoach" / "problems" / "1-two_sum.json").read_text("utf-8"))
    assert rec["id"] == "1-two_sum"
    assert (rec["number"], rec["title"], rec["difficulty"]) == (1, "Two Sum", "Easy")
    assert rec["pattern"] == "hash_map"
    assert rec["statement"] == LC_PASTE.strip()
    assert rec["notes"] == ""
    assert rec["review"] == {"box": 1, "due": (NOW.date() + timedelta(days=1)).isoformat(),
                             "history": []}
    assert rec["runs"] == ["answers/hash_map/1_two_sum__normal.md",
                           "answers/hash_map/1_two_sum__normal.py"]
    assert rec["created"] == NOW.isoformat(timespec="seconds")

    [entry] = ps.read_runs(root=root)
    assert entry == {
        "ts": NOW.isoformat(timespec="seconds"), "problem_id": "1-two_sum",
        "mode": "answer", "language": "python", "tier": "normal",
        "model": "claude-opus-5-5", "verdict": "pass",
        "files": ["answers/hash_map/1_two_sum__normal.md",
                  "answers/hash_map/1_two_sum__normal.py"],
        "session_id": "sess-1", "duration_s": 12.3, "pattern": "hash_map",
    }


def test_second_run_updates_record_without_losing_data(root):
    _record(root)
    later = NOW + timedelta(days=2)
    _record(root, paste="1. Two Sum\nshort", mode="learning", tier="", verdict=None,
            pattern="uncategorized", now=later,
            paths=[str(root / "learning/hash_map_learning/1_two_sum.md")])
    rec = ps.load_problem("1-two_sum", root=root)
    assert rec["pattern"] == "hash_map"          # a fallback never overwrites a real one
    assert rec["difficulty"] == "Easy"           # kept although the new paste lacks it
    assert rec["statement"] == LC_PASTE.strip()  # the fuller statement wins
    assert rec["created"] == NOW.isoformat(timespec="seconds")
    assert rec["updated"] == later.isoformat(timespec="seconds")
    assert rec["review"]["due"] == (NOW.date() + timedelta(days=1)).isoformat()
    assert rec["runs"][-1] == "learning/hash_map_learning/1_two_sum.md"
    assert len(rec["runs"]) == 3
    entries = ps.read_runs(root=root)
    assert [e["mode"] for e in entries] == ["answer", "learning"]
    assert entries[1]["verdict"] is None and entries[1]["tier"] is None


def test_difficulty_falls_back_to_the_doc_header(root):
    doc = "# 70. Climbing Stairs\nPattern: Dynamic Programming · Difficulty: Easy\n"
    pid = _record(root, paste="Climbing Stairs\nYou are climbing...", doc=doc,
                  pattern="dynamic_programming")
    assert pid == "climbing_stairs"
    rec = ps.load_problem(pid, root=root)
    assert rec["difficulty"] == "Easy"
    assert rec["number"] == 70


def test_paths_outside_root_are_kept_verbatim(root, tmp_path):
    pid = _record(root, paths=[str(tmp_path / "elsewhere.md")])
    assert ps.load_problem(pid, root=root)["runs"] == [str(tmp_path / "elsewhere.md")]


def test_corrupt_record_is_preserved_and_replaced(root):
    folder = root / ".leetcoach" / "problems"
    folder.mkdir(parents=True)
    (folder / "1-two_sum.json").write_text("{not json", encoding="utf-8")
    _record(root)
    assert ps.load_problem("1-two_sum", root=root)["title"] == "Two Sum"
    assert list(folder.glob("1-two_sum.json.corrupt-*"))


def test_list_problems_and_load_invalid(root):
    _record(root)
    _record(root, paste="Valid Parentheses\nMedium\n", pattern="stack")
    ids = [p["id"] for p in ps.list_problems(root=root)]
    assert sorted(ids) == ["1-two_sum", "valid_parentheses"]
    assert ps.load_problem("../../etc", root=root) is None
    assert ps.load_problem("nope", root=root) is None


def test_path_index_last_entry_wins(root):
    _record(root, verdict="fail")
    _record(root, verdict="pass")  # identical re-run: same files, newer verdict
    index = ps.path_index(root=root)
    hit = index["answers/hash_map/1_two_sum__normal.md"]
    assert hit["verdict"] == "pass"
    assert hit["problem_id"] == "1-two_sum"
    assert hit["difficulty"] == "Easy"
