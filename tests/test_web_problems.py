"""Problem records through the web layer: a saved run writes its problem
record + run-log line (and a run that saved nothing writes none), the
``/problems`` / ``/problems/<id>`` / ``/stats`` endpoints (list shape, merged
aliases, files that no longer exist), and the library listing's verdict /
difficulty / title coming from the run log and the records (doc fallback for
legacy files). Every Claude call is a fake; the sandbox is faked too."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from _helpers import freeze_stats_now, local_noon, parse_sse, write_problem_record

# A doc seeded with a logged study session (test_followup's fixtures), for the
# tests that delete a run's files and then read /problems.
from test_followup import PASTE as FOLLOWUP_PASTE
from test_followup import Recorder, root  # noqa: F401 - fixture
from test_followup import _seed as _seed_doc
from test_followup import _write as _write_doc

from leetcoach import app as app_module
from leetcoach import claude_cli, library, problem_store, sandbox

PASTE = ("1. Two Sum\nEasy\n\nGiven nums...\nExample 1:\n"
         "Input: nums = [2,7], target = 9\nOutput: [0,1]")
DOC = (
    "# 1. Two Sum\nPattern: Arrays & Hashing · Difficulty: Easy\n\n## Solution\n\n"
    "```python solution\nprint(input())\n```\n"
)


class Call:
    """Shaped like claude_cli.ClaudeRun: iterable + .model + .session_id."""

    def __init__(self, chunks, model="claude-opus-5-5", session_id="sess-42"):
        self._it = iter(chunks)
        self.model = model
        self.session_id = session_id

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)


def _run_fn(text=DOC, problem_type="hash_map"):
    def run_fn(prompt, **kwargs):
        if "Classify the following" in prompt:
            return iter([json.dumps({"problem_type": problem_type, "topics": ["hash map"]})])
        return Call([text[:7], text[7:]])
    return run_fn


@pytest.fixture
def out(tmp_path, monkeypatch):
    folder = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(folder))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(tmp_path / "topic_index.json"))
    monkeypatch.setattr(
        sandbox, "verify_answer",
        lambda code, problem, language, **kw: SimpleNamespace(
            status="pass", note="1/1 samples", detail=[]),
    )
    return folder


def _client(run_fn):
    application = app_module.create_app(
        run_fn=run_fn,
        auth_probe=lambda: claude_cli.AuthStatus(installed=True, logged_in=True),
    )
    application.config.update(TESTING=True)
    return application.test_client()


def _run(client, **body):
    payload = {"problem": PASTE, "mode": "answer", "language": "python", "tier": "normal"}
    payload.update(body)
    resp = client.post("/run", json=payload)
    _, events = parse_sse(resp.get_data(as_text=True))
    return events


# --- a run writes problems/<id>.json + a runs.jsonl line -----------------------------

def test_answer_run_records_problem_and_log_line(out):
    events = _run(_client(_run_fn()))
    name, done = events[-1]
    assert name == "done", events
    assert done["problem_id"] == "1-two_sum"

    rec = json.loads((out / ".leetcoach" / "problems" / "1-two_sum.json").read_text("utf-8"))
    assert (rec["number"], rec["title"], rec["difficulty"], rec["pattern"]) == (
        1, "Two Sum", "Easy", "hash_map")
    assert rec["statement"] == PASTE
    assert sorted(rec["runs"]) == ["answers/hash_map/1_two_sum__normal.md",
                                   "answers/hash_map/1_two_sum__normal.py"]

    [entry] = problem_store.read_runs(root=out)
    assert entry["problem_id"] == "1-two_sum"
    assert (entry["mode"], entry["language"], entry["tier"]) == ("answer", "python", "normal")
    assert entry["model"] == "claude-opus-5-5"
    assert entry["session_id"] == "sess-42"
    assert entry["verdict"] == "pass"
    assert sorted(entry["files"]) == sorted(rec["runs"])
    assert entry["pattern"] == "hash_map"
    assert isinstance(entry["duration_s"], float) and entry["duration_s"] >= 0


def test_learning_run_logs_no_tier_and_no_verdict(out):
    learning_doc = "# Two Sum\n\n## Approach\n\n### Hint 1\n\nThink about complements.\n"
    events = _run(_client(_run_fn(learning_doc)), mode="learning", tier="bogus")
    assert events[-1][0] == "done"
    [entry] = problem_store.read_runs(root=out)
    assert entry["mode"] == "learning"
    assert entry["tier"] is None and entry["verdict"] is None
    assert entry["files"] == ["learning/hash_map_learning/1_two_sum.md"]


def test_guided_run_logs_the_sandbox_verdict(out, monkeypatch):
    monkeypatch.setattr(
        sandbox, "verify_answer",
        lambda code, problem, language, **kw: SimpleNamespace(
            status="fail", note="0/1 samples passed", detail=[]),
    )
    events = _run(_client(_run_fn()), mode="guided")
    assert events[-1][0] == "done"
    [entry] = problem_store.read_runs(root=out)
    assert entry["verdict"] == "fail"


def test_a_failing_record_never_breaks_the_run(out, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(problem_store, "record_run", boom)
    events = _run(_client(_run_fn()))
    name, done = events[-1]
    assert name == "done" and "problem_id" not in done
    assert (out / "answers" / "hash_map" / "1_two_sum__normal.md").exists()


def test_a_failed_run_records_nothing(out):
    def run_fn(prompt, **kwargs):
        if "Classify the following" in prompt:
            return iter(['{"problem_type": "hash_map", "topics": []}'])
        return Call([])  # empty answer -> run error
    events = _run(_client(run_fn))
    assert events[-1][0] == "error"
    assert problem_store.read_runs(root=out) == []


# --- /problems ---------------------------------------------------------------------

def test_problems_endpoints(out):
    client = _client(_run_fn())
    _run(client)
    _run(client, mode="learning")
    listing = client.get("/problems").get_json()["problems"]
    assert [p["id"] for p in listing] == ["1-two_sum"]
    item = listing[0]
    assert item["difficulty"] == "Easy" and item["run_count"] == 2  # runs, not files
    assert "statement" not in item

    full = client.get("/problems/1-two_sum").get_json()
    assert full["statement"] == PASTE
    assert [e["mode"] for e in full["log"]] == ["answer", "learning"]

    assert client.get("/problems/nope").status_code == 404
    assert client.get("/problems/BAD..id").status_code == 404
    assert client.get("/problems/..%2F..%2Fetc").status_code == 404


def test_metadata_stays_hidden_from_the_library(out):
    client = _client(_run_fn())
    _run(client)
    paths = [f["path"] for f in client.get("/library").get_json()["files"]]
    assert not any(".leetcoach" in p for p in paths)
    assert client.get("/library/file?path=.leetcoach/runs.jsonl").status_code == 404
    assert client.get(
        "/library/file?path=.leetcoach/problems/1-two_sum.json").status_code == 404
    resp = client.delete("/library/file?path=.leetcoach/runs.jsonl")
    assert resp.status_code == 404


# --- /library: verdict + difficulty from the log, doc fallback for legacy -------------------

def _write(path, text, age_days=0, *, day: date | None = None):
    """Write ``path`` dated local noon on ``day`` (or ``age_days`` calendar
    days ago). 3A W9: calendar arithmetic, not ``age_days * 86400`` seconds,
    which lands on the wrong date across a 23 h / 25 h DST day."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if day is None:
        day = datetime.now().astimezone().date() - timedelta(days=age_days)
    stamp = local_noon(day)
    os.utime(path, (stamp, stamp))


def test_library_verdict_and_difficulty_from_the_log(out):
    client = _client(_run_fn())
    _run(client)
    # the saved doc says PASS; overwrite it so the doc and the log disagree
    md = out / "answers" / "hash_map" / "1_two_sum__normal.md"
    md.write_text(md.read_text("utf-8").replace("PASS", "FAIL"), encoding="utf-8")
    # a legacy doc with no log entry
    _write(out / "answers" / "stack" / "valid_parentheses__optimal.md",
           "# VP\n\n---\n\n**Verification:** ✗ Sample tests FAIL (0/1)\n", 1)
    # a fresh app has a cold listing cache
    files = {f["path"]: f for f in _client(_run_fn()).get("/library").get_json()["files"]}
    logged = files["answers/hash_map/1_two_sum__normal.md"]
    assert logged["verdict"] == "pass"            # the log wins over the doc text
    assert logged["difficulty"] == "Easy"
    assert logged["problem_id"] == "1-two_sum"
    assert files["answers/hash_map/1_two_sum__normal.py"]["difficulty"] == "Easy"
    legacy = files["answers/stack/valid_parentheses__optimal.md"]
    assert legacy["verdict"] == "fail"            # doc fallback
    assert "difficulty" not in legacy


# --- /stats --------------------------------------------------------------------------

def test_stats_endpoint_merges_log_and_legacy(out, monkeypatch):
    client = _client(_run_fn())
    _run(client)
    # 3A W9: the legacy doc is dated the calendar day before the logged run, and
    # /stats computes at the run's own time - a two-day streak whatever the clock.
    logged = datetime.fromisoformat(problem_store.read_runs()[-1]["ts"]).astimezone()
    _write(out / "guided" / "stack" / "valid_parentheses.md", "# VP\n",
           day=logged.date() - timedelta(days=1))
    freeze_stats_now(monkeypatch, logged)
    s = _client(_run_fn()).get("/stats").get_json()
    assert s["total"] == 2
    assert s["sources"] == {"log": 1, "legacy": 1}
    assert s["currentStreak"] == 2
    assert s["byMode"] == {"Answer": 1, "Guided": 1}
    assert len(s["heatmap"]) == 119


def test_stats_on_an_empty_library(out):
    s = _client(_run_fn()).get("/stats").get_json()
    assert s["total"] == 0 and s["currentStreak"] == 0


# --- D2: the parsed paste feeds the doc contract --------------------------------------

@pytest.mark.parametrize("mode", ["answer", "learning", "guided"])
def test_study_prompt_carries_the_parsed_number_and_difficulty(out, mode):
    seen = []

    def run_fn(prompt, **kwargs):
        if "Classify the following" in prompt:
            return iter(['{"problem_type": "hash_map", "topics": []}'])
        seen.append(prompt)
        return Call([DOC])

    _run(_client(run_fn), mode=mode)
    [prompt] = seen
    assert "OUTPUT CONTRACT" in prompt
    assert "`# 1. <Title>`" in prompt
    assert "`Difficulty: Easy`" in prompt


# --- O1: the /problems list shape; merged aliases in the detail log ----------

def test_problems_list_has_run_count_file_count_and_paths(out):
    client = _client(_run_fn())
    _run(client)                    # answer: .md + .py
    _run(client, mode="learning")   # one .md
    [item] = client.get("/problems").get_json()["problems"]
    assert item["run_count"] == 2   # saved runs (log entries)
    assert item["file_count"] == 3  # files those runs produced
    assert sorted(item["runs"]) == sorted(
        problem_store.load_problem("1-two_sum", root=out)["runs"])
    assert "statement" not in item


def test_problem_detail_log_includes_merged_aliases(out):
    client = _client(_run_fn())
    _run(client, problem="Two Sum\nGiven nums...\nExample 1:\nInput: nums = [2,7], "
         "target = 9\nOutput: [0,1]")
    _run(client)  # "1. Two Sum" - merges the un-numbered record
    [item] = client.get("/problems").get_json()["problems"]
    assert item["id"] == "1-two_sum" and item["run_count"] == 2
    full = client.get("/problems/1-two_sum").get_json()
    assert [e["problem_id"] for e in full["log"]] == ["two_sum", "1-two_sum"]
    assert client.get("/problems/two_sum").get_json()["id"] == "1-two_sum"


@pytest.fixture
def application(root):  # noqa: F811 - fixture
    def no_claude(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("no Claude call expected")

    application = app_module.create_app(
        run_fn=no_claude,
        auth_probe=lambda: claude_cli.AuthStatus(installed=True, logged_in=True))
    application.config.update(TESTING=True)
    return application


# --- 3A W3: GET /problems keeps only string aliases from a hand-edited record ------

@pytest.mark.parametrize("raw,expected", [
    ([{"x": 1}, "two_sum", 5, ["y"]], ["two_sum"]),
    ("two_sum", []),
    ({"two_sum": 1}, []),
    (7, []),
])
def test_problems_listing_filters_non_string_aliases(root, application, raw, expected):  # noqa: F811 - fixture
    write_problem_record(root, "1-two_sum", number=1, aliases=raw)
    resp = application.test_client().get("/problems")
    assert resp.status_code == 200
    (item,) = resp.get_json()["problems"]
    assert item["aliases"] == expected
    assert item["run_count"] == 0


# --- M7: /problems counts only files that still exist -------------------------------

def test_problems_drop_deleted_files_from_runs_and_file_count(root):  # noqa: F811 - fixture
    rel = _seed_doc(root)
    code = _write_doc(root, "answers/hash_map/1_two_sum__normal.py", "print(1)\n")
    problem_store.record_run(
        FOLLOWUP_PASTE, mode="learning", language="python", tier=None,
        model="m", verdict=None, paths=[code], session_id=None, duration_s=1.0,
        pattern="hash_map", doc="x", root=root)
    c = _client(Recorder())
    [item] = c.get("/problems").get_json()["problems"]
    assert item["file_count"] == 2
    assert c.delete("/library/file", query_string={"path": rel, "scope": "run"}).status_code == 200
    [item] = c.get("/problems").get_json()["problems"]
    assert item["runs"] == [] and item["file_count"] == 0
    assert item["run_count"] == 2  # the run log is append-only (A8)
    detail = c.get("/problems/1-two_sum")
    assert detail.status_code == 200  # Practice still opens
    body = detail.get_json()
    assert body["runs"] == [] and len(body["log"]) == 2
    # the record on disk is untouched (a restored file reappears)
    assert len(problem_store.load_problem("1-two_sum", root=root)["runs"]) == 2


def test_problems_listing_reads_existence_from_the_library_listing(root, monkeypatch):  # noqa: F811 - fixture
    # 3B 3.5: /problems used to resolve every record's every run path (three
    # realpath calls each): ~300 ms per call on a 500-problem library. Files
    # the cached listing already holds cost no filesystem call; only a path
    # it lacks is resolved.
    _seed_doc(root)
    code = _write_doc(root, "answers/hash_map/1_two_sum__normal.py", "print(1)\n")
    problem_store.record_run(
        FOLLOWUP_PASTE, mode="learning", language="python", tier=None,
        model="m", verdict=None, paths=[code], session_id=None, duration_s=1.0,
        pattern="hash_map", doc="x", root=root)
    resolved = []
    real = library.resolve_file

    def counting(rel):
        resolved.append(rel)
        return real(rel)

    monkeypatch.setattr(library, "resolve_file", counting)
    c = _client(Recorder())
    [item] = c.get("/problems").get_json()["problems"]
    assert item["file_count"] == 2
    assert resolved == []
    # a file removed outside the app (Explorer) still drops out
    code.unlink()
    [item] = c.get("/problems").get_json()["problems"]
    assert item["runs"] == ["answers/hash_map/1_two_sum__normal.md"]
    assert item["file_count"] == 1


# --- O2 / M8: what a saved run logs --------------------------------------------

def test_guided_run_logs_no_tier(out):
    _run(_client(_run_fn()), mode="guided", tier="optimal")
    [entry] = problem_store.read_runs(root=out)
    assert entry["mode"] == "guided" and entry["tier"] is None


def test_a_run_that_saved_nothing_is_not_logged(out, monkeypatch):
    from leetcoach import storage

    monkeypatch.setattr(storage, "save_answer", lambda *a, **k: (None, None))
    events = _run(_client(_run_fn()))
    assert events[-1][0] == "done"
    assert problem_store.read_runs(root=out) == []
    assert "problem_id" not in events[-1][1]


# --- M4 / O3: the library listing reads the run log and the records -----------

def test_listing_sees_an_external_log_append(out):
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


def test_listing_carries_the_record_title_and_number(out):
    client = _client(_run_fn())
    _run(client)
    files = {f["path"]: f for f in client.get("/library").get_json()["files"]}
    hit = files["answers/hash_map/1_two_sum__normal.md"]
    assert (hit["title"], hit["number"]) == ("Two Sum", 1)
