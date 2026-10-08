"""SP6 fix round, web side: the /problems list shape (O1), no tier outside
Answer (O2), the listing cache seeing external run-log appends (M4), runs
that saved nothing (M8) and record titles in the library listing (O3)."""
from __future__ import annotations

import json

from test_web_sp6 import _client, _run, _run_fn, out  # noqa: F401 - fixture

import problem_store


def test_problems_list_has_run_count_file_count_and_paths(out):  # noqa: F811
    client = _client(_run_fn())
    _run(client)                    # answer: .md + .py
    _run(client, mode="learning")   # one .md
    [item] = client.get("/problems").get_json()["problems"]
    assert item["run_count"] == 2   # saved runs (log entries)
    assert item["file_count"] == 3  # files those runs produced
    assert sorted(item["runs"]) == sorted(
        problem_store.load_problem("1-two_sum", root=out)["runs"])
    assert "statement" not in item


def test_problem_detail_log_includes_merged_aliases(out):  # noqa: F811
    client = _client(_run_fn())
    _run(client, problem="Two Sum\nGiven nums...\nExample 1:\nInput: nums = [2,7], "
         "target = 9\nOutput: [0,1]")
    _run(client)  # "1. Two Sum" - merges the un-numbered record
    [item] = client.get("/problems").get_json()["problems"]
    assert item["id"] == "1-two_sum" and item["run_count"] == 2
    full = client.get("/problems/1-two_sum").get_json()
    assert [e["problem_id"] for e in full["log"]] == ["two_sum", "1-two_sum"]
    assert client.get("/problems/two_sum").get_json()["id"] == "1-two_sum"


def test_guided_run_logs_no_tier(out):  # noqa: F811
    _run(_client(_run_fn()), mode="guided", tier="optimal")
    [entry] = problem_store.read_runs(root=out)
    assert entry["mode"] == "guided" and entry["tier"] is None


def test_listing_sees_an_external_log_append(out):  # noqa: F811
    client = _client(_run_fn())
    _run(client)
    md = "answers/hash_map/1_two_sum__normal.md"
    files = {f["path"]: f for f in client.get("/library").get_json()["files"]}
    assert files[md]["verdict"] == "pass"
    # another process (or a hand edit) appends a newer entry for the same file
    entry = dict(problem_store.read_runs(root=out)[-1], verdict="fail")
    with open(out / ".leetcoach" / "runs.jsonl", "ab") as fh:
        fh.write((json.dumps(entry) + "\n").encode("utf-8"))
    files = {f["path"]: f for f in client.get("/library").get_json()["files"]}
    assert files[md]["verdict"] == "fail"


def test_listing_carries_the_record_title_and_number(out):  # noqa: F811
    client = _client(_run_fn())
    _run(client)
    files = {f["path"]: f for f in client.get("/library").get_json()["files"]}
    hit = files["answers/hash_map/1_two_sum__normal.md"]
    assert (hit["title"], hit["number"]) == ("Two Sum", 1)


def test_a_run_that_saved_nothing_is_not_logged(out, monkeypatch):  # noqa: F811
    import storage

    monkeypatch.setattr(storage, "save_answer", lambda *a, **k: (None, None))
    events = _run(_client(_run_fn()))
    assert events[-1][0] == "done"
    assert problem_store.read_runs(root=out) == []
    assert "problem_id" not in events[-1][1]
