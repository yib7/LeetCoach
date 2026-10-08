"""SP6 web wiring: a saved run writes its problem record + run-log line, the
``/problems`` / ``/problems/<id>`` / ``/stats`` endpoints, and the library
listing's verdict / difficulty coming from the run log (doc fallback for
legacy files). Every Claude call is a fake; the sandbox is faked too."""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest
from _helpers import parse_sse

import app as app_module
import claude_cli
import problem_store
import sandbox

PASTE = "1. Two Sum\nEasy\n\nGiven nums...\nExample 1:\nInput: nums = [2,7], target = 9\nOutput: [0,1]"
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
    root = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(root))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(tmp_path / "topic_index.json"))
    monkeypatch.setattr(
        sandbox, "verify_answer",
        lambda code, problem, language, **kw: SimpleNamespace(
            status="pass", note="1/1 samples", detail=[]),
    )
    return root


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
    assert item["difficulty"] == "Easy" and item["run_count"] == 3
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

def _write(path, text, age_days=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age_days * 86400
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
    files ={f["path"]: f for f in _client(_run_fn()).get("/library").get_json()["files"]}
    logged = files["answers/hash_map/1_two_sum__normal.md"]
    assert logged["verdict"] == "pass"            # the log wins over the doc text
    assert logged["difficulty"] == "Easy"
    assert logged["problem_id"] == "1-two_sum"
    assert files["answers/hash_map/1_two_sum__normal.py"]["difficulty"] == "Easy"
    legacy = files["answers/stack/valid_parentheses__optimal.md"]
    assert legacy["verdict"] == "fail"            # doc fallback
    assert "difficulty" not in legacy


# --- /stats --------------------------------------------------------------------------

def test_stats_endpoint_merges_log_and_legacy(out):
    client = _client(_run_fn())
    _run(client)
    _write(out / "guided" / "stack" / "valid_parentheses.md", "# VP\n", 1)
    s = _client(_run_fn()).get("/stats").get_json()
    assert s["total"] == 2
    assert s["sources"] == {"log": 1, "legacy": 1}
    assert s["currentStreak"] == 2
    assert s["byMode"] == {"Answer": 1, "Guided": 1}
    assert len(s["heatmap"]) == 119


def test_stats_on_an_empty_library(out):
    s = _client(_run_fn()).get("/stats").get_json()
    assert s["total"] == 0 and s["currentStreak"] == 0

