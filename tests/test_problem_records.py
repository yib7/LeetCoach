"""How ``problem_store`` files runs into problem records, past the basics in
``test_problem_store``: where a record's difficulty comes from (M1), folding an
un-numbered record into its numbered twin - runs, aliases, review schedule,
notes (M1, SP7 fix 7) - titles and ids from generic first lines (M2), when a
run is logged at all (M3, M8, O2), and the cached run log / record index that
external edits invalidate (M4)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest
from _helpers import write_problem_record as _write

from leetcoach import atomic_files
from leetcoach import problem_store as ps

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

    monkeypatch.setattr(atomic_files, "atomic_write_text", boom)
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


# --- SP7 fix 7: merging an un-numbered record into a numbered one keeps the
# review schedule and caps the notes

PASTE = (
    "1. Two Sum\nEasy\n\nGiven nums.\n\n"
    "Example 1:\nInput: nums = [2,7,11,15], target = 9\nOutput: [0,1]\n"
)


def _seed(root, paste=PASTE):
    return ps.record_run(
        paste, mode="answer", language="python", tier="normal", model="m", verdict="pass",
        paths=[root / "answers/hash_map/two_sum__normal.md"], session_id=None,
        duration_s=1.0, pattern="hash_map", root=root)


def _read(root, pid):
    return json.loads((root / ".leetcoach/problems" / f"{pid}.json").read_text("utf-8"))


def _merge(root):
    """A numbered paste folds the un-numbered ``two_sum`` into ``1-two_sum``."""
    pid = _seed(root)
    assert pid == "1-two_sum"
    assert not (root / ".leetcoach/problems/two_sum.json").exists()
    return _read(root, pid)


def _h(day, grade, box):
    return {"ts": f"{day}T09:00:00", "day": day, "grade": grade, "from_box": 1, "box": box,
            "due": day}


def test_merge_keeps_the_more_recently_reviewed_schedule(root):
    _write(root, "two_sum", review={
        "box": 4, "due": "2026-03-01", "last_reviewed": "2026-02-15T09:00:00",
        "history": [_h("2026-02-01", "solo", 3), _h("2026-02-15", "solo", 4)]})
    _write(root, "1-two_sum", number=1, review={
        "box": 2, "due": "2026-02-10", "last_reviewed": "2026-02-07T09:00:00",
        "history": [_h("2026-02-07", "solo", 2)]})
    rec = _merge(root)
    assert rec["review"]["box"] == 4 and rec["review"]["due"] == "2026-03-01"
    assert rec["review"]["last_reviewed"] == "2026-02-15T09:00:00"
    assert [h["day"] for h in rec["review"]["history"]] == [
        "2026-02-01", "2026-02-07", "2026-02-15"]


def test_merge_keeps_the_numbered_schedule_when_it_is_newer(root):
    _write(root, "two_sum", review={"box": 1, "due": "2026-01-02", "history": []})
    _write(root, "1-two_sum", number=1, review={
        "box": 3, "due": "2026-02-20", "last_reviewed": "2026-02-13T09:00:00",
        "history": [_h("2026-02-13", "solo", 3)]})
    rec = _merge(root)
    assert (rec["review"]["box"], rec["review"]["due"]) == (3, "2026-02-20")
    assert len(rec["review"]["history"]) == 1


def test_merge_history_is_deduplicated_and_capped(root, monkeypatch):
    monkeypatch.setattr(ps, "REVIEW_HISTORY_CAP", 3)
    shared = _h("2026-02-01", "solo", 2)
    _write(root, "two_sum", review={
        "box": 2, "due": "2026-02-04", "last_reviewed": "2026-02-03T09:00:00",
        "history": [shared, _h("2026-02-02", "hints", 2), _h("2026-02-03", "hints", 2)]})
    _write(root, "1-two_sum", number=1, review={
        "box": 1, "due": "2026-01-31", "last_reviewed": "2026-02-01T09:00:00",
        "history": [_h("2026-01-30", "peeked", 1), shared]})
    rec = _merge(root)
    assert [h["day"] for h in rec["review"]["history"]] == [
        "2026-02-01", "2026-02-02", "2026-02-03"]


def test_merged_notes_are_capped_with_a_marker(root):
    _write(root, "two_sum", notes="a" * 15_000)
    _write(root, "1-two_sum", number=1, notes="b" * 15_000)
    rec = _merge(root)
    assert len(rec["notes"]) == ps.NOTES_CAP
    assert rec["notes"].startswith("a" * 100)
    assert rec["notes"].endswith(ps.MERGED_NOTES_MARKER)
    # a later notes save of the capped text still fits the endpoint's cap
    assert ps.set_notes("1-two_sum", rec["notes"], root=root) is not None


def test_short_merged_notes_are_joined_unchanged(root):
    _write(root, "two_sum", notes="first")
    _write(root, "1-two_sum", number=1, notes="second")
    assert _merge(root)["notes"] == "first\n\nsecond"
