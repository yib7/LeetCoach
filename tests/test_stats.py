"""SP6 / A8: Stats computed from the run log, with legacy library files that
have no log entry counted per run by their own mtime."""
from __future__ import annotations

from datetime import datetime, timedelta

import stats

NOW = datetime(2026, 10, 8, 18, 0)  # local, naive


def _day(n_ago: int, hour: int = 10) -> datetime:
    return (NOW - timedelta(days=n_ago)).replace(hour=hour)


def _entry(n_ago, *, pid="1-two_sum", mode="answer", lang="python", tier="normal",
           files=(), pattern="hash_map", hour=10):
    return {
        "ts": _day(n_ago, hour).astimezone().isoformat(timespec="seconds"),
        "problem_id": pid, "mode": mode, "language": lang, "tier": tier,
        "verdict": "pass", "files": list(files), "pattern": pattern,
    }


def _file(path, n_ago, hour=10):
    return {"path": path, "size": 10, "mtime": _day(n_ago, hour).timestamp()}


def test_a8_four_day_scenario_from_the_log():
    entries = [
        _entry(3, files=["answers/hash_map/two_sum__normal.md"]),
        _entry(2, tier="optimal", files=["answers/hash_map/two_sum__optimal.md"]),
        _entry(2, tier="normal", hour=15, files=["answers/hash_map/two_sum__normal__2.md"]),
        _entry(1, files=["answers/hash_map/two_sum__normal__3.md"]),
        _entry(0, mode="learning", tier=None, files=["learning/hash_map_learning/two_sum.md"]),
    ]
    s = stats.compute_stats(entries, [], now=NOW)
    assert s["currentStreak"] == 4
    assert s["longestStreak"] == 4
    assert s["total"] == 5
    assert s["today"] == 1
    assert s["thisWeek"] == 5
    assert s["distinctProblems"] == 1
    assert s["byMode"] == {"Answer": 4, "Learning": 1}
    assert s["byLanguage"] == {"Python": 5}
    assert s["byTopic"] == {"Hash Map": 5}
    assert s["sources"] == {"log": 5, "legacy": 0}


def test_a8_four_day_scenario_from_legacy_files():
    # The old client grouped by the stem before the first "__", so these all
    # collapsed into ONE run (streak 1). Each tier / slot is its own run, and
    # an Answer's .md + .py pair is one run.
    files = [
        _file("answers/hash_map/two_sum__normal.md", 3),
        _file("answers/hash_map/two_sum__normal.py", 3),
        _file("answers/hash_map/two_sum__optimal.md", 2),
        _file("answers/hash_map/two_sum__normal__2.md", 2, hour=16),
        _file("answers/hash_map/two_sum__normal__3.md", 1),
        _file("learning/hash_map_learning/two_sum.md", 0),
    ]
    s = stats.compute_stats([], files, now=NOW)
    assert s["currentStreak"] == 4
    assert s["total"] == 5
    assert s["byMode"] == {"Answer": 4, "Learning": 1}
    assert s["byLanguage"] == {"Python": 1, "Unknown": 4}
    assert s["distinctProblems"] == 1
    assert s["sources"] == {"log": 0, "legacy": 5}


def test_logged_files_are_not_counted_twice_and_legacy_fills_the_gaps():
    entries = [_entry(0, files=["answers/hash_map/two_sum__normal.md",
                                "answers/hash_map/two_sum__normal.py"])]
    files = [
        _file("answers/hash_map/two_sum__normal.md", 0),
        _file("answers/hash_map/two_sum__normal.py", 0),
        _file("guided/stack/valid_parentheses.md", 1),   # legacy, no entry
        _file("_unsorted/abc.md", 2),                     # not a run folder
        _file("notes.md", 2),
    ]
    s = stats.compute_stats(entries, files, now=NOW)
    assert s["total"] == 2
    assert s["currentStreak"] == 2
    assert s["byMode"] == {"Answer": 1, "Guided": 1}
    assert s["byTopic"] == {"Hash Map": 1, "Stack": 1}
    assert s["distinctProblems"] == 2


def test_deleted_files_still_count_from_the_log():
    entries = [_entry(0, files=["answers/hash_map/gone.md"])]
    assert stats.compute_stats(entries, [], now=NOW)["total"] == 1


def test_streak_counts_from_yesterday_and_breaks_on_a_gap():
    entries = [_entry(1), _entry(2), _entry(4)]
    s = stats.compute_stats(entries, [], now=NOW)
    assert s["currentStreak"] == 2
    assert s["today"] == 0
    s = stats.compute_stats([_entry(2), _entry(3)], [], now=NOW)
    assert s["currentStreak"] == 0
    assert s["longestStreak"] == 2


def test_labels_and_distinct_problem_keys():
    entries = [
        _entry(0, pid="1-two_sum", lang="cpp", pattern="two_pointers"),
        _entry(0, pid="two_sum", lang="java", pattern="uncategorized"),
        _entry(0, pid="20-valid_parentheses", mode="guided", pattern="stack"),
        {"ts": "garbage", "mode": "answer"},          # unparsable time: skipped
        {"ts": 12.5, "mode": "answer"},                # epoch numbers are fine
    ]
    s = stats.compute_stats(entries, [], now=NOW)
    assert s["total"] == 4
    assert s["byLanguage"] == {"C++": 1, "Java": 1, "Python": 1, "Unknown": 1}
    assert s["byTopic"]["Two Pointers"] == 1
    assert s["byTopic"]["Uncategorized"] == 2
    assert s["distinctProblems"] == 2  # 1-two_sum and two_sum are one problem


def test_heatmap_covers_17_weeks_ending_today():
    s = stats.compute_stats([_entry(0), _entry(0, hour=11), _entry(118)], [], now=NOW)
    hm = s["heatmap"]
    assert len(hm) == 119
    assert hm[-1] == {"date": "2026-10-08", "count": 2}
    assert hm[0]["count"] == 1


def test_empty():
    s = stats.compute_stats([], [], now=NOW)
    assert s["total"] == 0 and s["currentStreak"] == 0 and len(s["heatmap"]) == 119
