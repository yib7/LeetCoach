"""SP7 / D4 + D9: Leitner scheduling math, grading a problem record, the
review summary, and per-problem notes (problem_store)."""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta

import pytest

import problem_store as ps

PASTE = "1. Two Sum\nEasy\n\nGiven nums...\nExample 1:\nInput: nums = [2,7], target = 9\nOutput: [0,1]"


@pytest.fixture
def root(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return out


def _record(root, paste=PASTE, *, now=datetime(2026, 1, 10, 9, 0)):
    return ps.record_run(
        paste, mode="answer", language="python", tier="normal", model="m",
        verdict="pass", paths=[root / "answers/x/two_sum__normal.md"], session_id=None,
        duration_s=1.0, pattern="hash_map", now=now, root=root)


def _read(root, pid):
    return json.loads((root / ".leetcoach/problems" / f"{pid}.json").read_text("utf-8"))


# --- the pure Leitner rule -----------------------------------------------------------

D = date(2026, 3, 10)


@pytest.mark.parametrize("box,grade,new_box,days", [
    (1, "solo", 2, 3), (2, "solo", 3, 7), (3, "solo", 4, 14), (4, "solo", 5, 30),
    (5, "solo", 5, 30),                      # capped at the last box
    (1, "hints", 1, 1), (3, "hints", 3, 7), (5, "hints", 5, 30),
    (1, "peeked", 1, 1), (4, "peeked", 1, 1), (5, "peeked", 1, 1),
])
def test_every_leitner_transition(box, grade, new_box, days):
    assert ps.next_review(box, grade, D) == (new_box, D + timedelta(days=days))


@pytest.mark.parametrize("stored,expected", [
    (0, 1), (-3, 1), (6, 5), (99, 5), (None, 1), ("3", 1), (True, 1), (2.0, 1), (4, 4),
])
def test_box_is_clamped_to_a_valid_box(stored, expected):
    assert ps.clamp_box(stored) == expected


def test_out_of_range_boxes_still_schedule_sanely():
    assert ps.next_review(9, "solo", D) == (5, D + timedelta(days=30))
    assert ps.next_review(0, "hints", D) == (1, D + timedelta(days=1))
    assert ps.next_review("junk", "solo", D) == (2, D + timedelta(days=3))


def test_unknown_grade_is_rejected():
    with pytest.raises(ValueError):
        ps.next_review(1, "great", D)


@pytest.mark.parametrize("today,box,grade,due", [
    (date(2026, 1, 31), 1, "hints", date(2026, 2, 1)),     # month end
    (date(2026, 1, 30), 1, "solo", date(2026, 2, 2)),
    (date(2026, 2, 28), 1, "hints", date(2026, 3, 1)),     # non-leap February
    (date(2028, 2, 28), 1, "hints", date(2028, 2, 29)),    # leap day
    (date(2026, 12, 31), 1, "peeked", date(2027, 1, 1)),   # year end
    (date(2026, 12, 20), 4, "solo", date(2027, 1, 19)),    # 30 days across the year
    (date(2026, 4, 30), 3, "solo", date(2026, 5, 14)),
])
def test_date_arithmetic_across_month_and_year_ends(today, box, grade, due):
    assert ps.next_review(box, grade, today)[1] == due


def test_local_today_uses_the_local_calendar_day():
    late = datetime(2026, 1, 31, 23, 59, 59)          # naive = local already
    early = datetime(2026, 2, 1, 0, 0, 1)
    assert ps.local_today(late) == date(2026, 1, 31)
    assert ps.local_today(early) == date(2026, 2, 1)
    # an aware datetime is converted to the local zone first
    aware = late.astimezone()
    assert ps.local_today(aware) == date(2026, 1, 31)


# --- grading a record ----------------------------------------------------------------

def test_a_new_record_starts_in_box_one_due_the_next_day(root):
    pid = _record(root)
    assert pid == "1-two_sum"
    assert _read(root, pid)["review"]["box"] == 1
    assert _read(root, pid)["review"]["due"] == "2026-01-11"


def test_grading_moves_the_box_and_due_date_and_keeps_history(root):
    pid = _record(root)
    t1 = datetime(2026, 1, 31, 23, 59)
    res = ps.grade_problem(pid, "solo", now=t1, root=root)
    assert res["id"] == pid
    assert res["previous"] == {"box": 1, "due": "2026-01-11"}
    assert res["review"]["box"] == 2 and res["review"]["due"] == "2026-02-03"
    # one minute later is the next local day
    t2 = datetime(2026, 2, 1, 0, 0)
    res = ps.grade_problem(pid, "solo", now=t2, root=root,
                           attempt={"language": "python", "passed": 2, "total": 2})
    assert res["review"]["box"] == 3 and res["review"]["due"] == "2026-02-08"
    res = ps.grade_problem(pid, "hints", now=t2, root=root)
    assert res["review"]["box"] == 3 and res["review"]["due"] == "2026-02-08"
    res = ps.grade_problem(pid, "peeked", now=t2, root=root)
    assert res["review"]["box"] == 1 and res["review"]["due"] == "2026-02-02"
    rec = _read(root, pid)
    hist = rec["review"]["history"]
    assert [h["grade"] for h in hist] == ["solo", "solo", "hints", "peeked"]
    assert [(h["from_box"], h["box"]) for h in hist] == [(1, 2), (2, 3), (3, 3), (3, 1)]
    assert hist[0]["day"] == "2026-01-31" and hist[1]["day"] == "2026-02-01"
    assert hist[1]["attempt"] == {"language": "python", "passed": 2, "total": 2}
    assert rec["review"]["last_reviewed"].startswith("2026-02-01T00:00")
    # the rest of the record is untouched
    assert rec["title"] == "Two Sum" and rec["runs"] == ["answers/x/two_sum__normal.md"]


def test_box_is_capped_after_many_solo_grades(root):
    pid = _record(root)
    day = datetime(2026, 5, 1, 12, 0)
    for _ in range(8):
        res = ps.grade_problem(pid, "solo", now=day, root=root)
    assert res["review"]["box"] == 5
    assert res["review"]["due"] == "2026-05-31"


def test_history_is_capped(root, monkeypatch):
    monkeypatch.setattr(ps, "REVIEW_HISTORY_CAP", 3)
    pid = _record(root)
    for _ in range(5):
        ps.grade_problem(pid, "hints", now=datetime(2026, 5, 1), root=root)
    assert len(_read(root, pid)["review"]["history"]) == 3


def test_grade_unknown_problem_or_bad_input(root):
    assert ps.grade_problem("9-nope", "solo", root=root) is None
    assert ps.grade_problem("../etc", "solo", root=root) is None
    pid = _record(root)
    with pytest.raises(ValueError):
        ps.grade_problem(pid, "meh", root=root)


def test_grade_through_an_alias_updates_the_merged_record(root):
    ps.record_run("Two Sum\n\nGiven nums", mode="learning", language="python", tier=None,
                  model=None, verdict=None, paths=[root / "learning/a/two_sum.md"],
                  session_id=None, duration_s=None, pattern="hash_map",
                  now=datetime(2026, 1, 1), root=root)
    _record(root)  # the numbered paste folds `two_sum` into `1-two_sum`
    res = ps.grade_problem("two_sum", "solo", now=datetime(2026, 1, 20), root=root)
    assert res["id"] == "1-two_sum"
    assert _read(root, "1-two_sum")["review"]["box"] == 2


def test_a_legacy_record_without_review_can_be_graded(root):
    pid = _record(root)
    path = root / ".leetcoach/problems" / f"{pid}.json"
    rec = json.loads(path.read_text("utf-8"))
    del rec["review"]
    path.write_text(json.dumps(rec), "utf-8")
    res = ps.grade_problem(pid, "solo", now=datetime(2026, 6, 1), root=root)
    assert res["previous"] == {"box": 1, "due": None}
    assert res["review"]["box"] == 2 and res["review"]["due"] == "2026-06-04"


def test_concurrent_grades_never_lose_an_update(root):
    pid = _record(root)
    threads = [threading.Thread(target=ps.grade_problem, args=(pid, "hints"),
                                kwargs={"root": root}) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_read(root, pid)["review"]["history"]) == 8


# --- the review summary ----------------------------------------------------------------

def _set_review(root, pid, **review):
    path = root / ".leetcoach/problems" / f"{pid}.json"
    rec = json.loads(path.read_text("utf-8"))
    rec["review"].update(review)
    path.write_text(json.dumps(rec), "utf-8")


def test_summary_lists_due_problems_and_counts(root):
    a = _record(root)
    b = _record(root, "20. Valid Parentheses\nEasy\n\nGiven s")
    c = _record(root, "42. Trapping Rain Water\nHard\n\nGiven h")
    _set_review(root, a, box=2, due="2026-03-01")
    _set_review(root, b, box=1, due="2026-03-05")
    _set_review(root, c, box=4, due="2026-03-20")
    now = datetime(2026, 3, 5, 8, 0)
    ps.grade_problem(c, "hints", now=now, root=root)   # reviewed today, stays not due
    s = ps.review_summary(now=now, root=root)
    assert s["today"] == "2026-03-05"
    assert [i["id"] for i in s["due"]] == [a, b]       # oldest due first
    assert s["due"][0]["overdue_days"] == 4 and s["due"][1]["overdue_days"] == 0
    assert s["due"][0]["title"] == "Two Sum" and s["due"][0]["box"] == 2
    assert s["counts"]["due"] == 2
    assert s["counts"]["reviewed_today"] == 1
    assert s["counts"]["scheduled"] == 3
    assert s["counts"]["by_box"] == {"1": 1, "2": 1, "3": 0, "4": 1, "5": 0}
    assert s["next_due"] == "2026-03-19"               # box 4 hints -> +14 days


def test_summary_due_boundary_is_the_local_day(root):
    pid = _record(root)
    _set_review(root, pid, due="2026-02-01")
    assert ps.review_summary(now=datetime(2026, 1, 31, 23, 59), root=root)["counts"]["due"] == 0
    assert ps.review_summary(now=datetime(2026, 2, 1, 0, 0), root=root)["counts"]["due"] == 1


def test_summary_treats_a_missing_or_bad_due_as_due(root):
    pid = _record(root)
    _set_review(root, pid, due=None, box="x")
    s = ps.review_summary(now=datetime(2026, 1, 1), root=root)
    assert s["due"][0]["id"] == pid and s["due"][0]["due"] is None
    assert s["due"][0]["box"] == 1


def test_summary_of_an_empty_store(root):
    s = ps.review_summary(now=datetime(2026, 1, 1), root=root)
    assert s["due"] == [] and s["next_due"] is None
    assert s["counts"] == {"due": 0, "reviewed_today": 0, "scheduled": 0,
                           "by_box": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}}


# --- notes (D9) ------------------------------------------------------------------------

def test_notes_persist_in_the_record(root):
    pid = _record(root)
    rec = ps.set_notes(pid, "remember: check before insert\n✓ unicode",
                       now=datetime(2026, 1, 2, 3, 4), root=root)
    assert rec["notes"].startswith("remember")
    stored = _read(root, pid)
    assert stored["notes"] == "remember: check before insert\n✓ unicode"
    assert stored["notes_updated"].startswith("2026-01-02T03:04")
    # a later run keeps the notes
    _record(root)
    assert _read(root, pid)["notes"].startswith("remember")


def test_notes_for_unknown_problem(root):
    assert ps.set_notes("nope", "x", root=root) is None
    assert ps.set_notes("..\\x", "x", root=root) is None
