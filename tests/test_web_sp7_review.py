"""SP7 web: ``GET /review``, ``POST /problems/<id>/grade`` and
``PUT /problems/<id>/notes`` - input validation, the Leitner move the
endpoint applies, and notes persistence. No Claude call is made."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import app as app_module
import claude_cli
import problem_store

PASTE = "1. Two Sum\nEasy\n\nGiven nums...\nExample 1:\nInput: nums = [2,7], target = 9\nOutput: [0,1]"


# 3A W9: the routes' "today" is frozen, so the expected dates below never
# disagree with the server's own clock read (a run crossing local midnight).
TODAY = date(2026, 3, 10)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(problem_store, "local_today", lambda now=None: TODAY)
    return TODAY


@pytest.fixture
def root(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(out))
    return out


@pytest.fixture
def client(root):
    def no_claude(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("no Claude call expected")

    application = app_module.create_app(
        run_fn=no_claude,
        auth_probe=lambda: claude_cli.AuthStatus(installed=True, logged_in=True))
    application.config.update(TESTING=True)
    return application.test_client()


def _seed(root, paste=PASTE):
    return problem_store.record_run(
        paste, mode="answer", language="python", tier="normal", model="m", verdict="pass",
        paths=[root / "answers/hash_map/two_sum__normal.md"], session_id=None,
        duration_s=1.0, pattern="hash_map", root=root)


def _set_due(root, pid, due, box=1):
    path = root / ".leetcoach/problems" / f"{pid}.json"
    rec = json.loads(path.read_text("utf-8"))
    rec["review"].update(due=due, box=box)
    path.write_text(json.dumps(rec), "utf-8")


# --- GET /review ------------------------------------------------------------------------

def test_review_empty_library(client):
    data = client.get("/review").get_json()
    assert data["due"] == [] and data["counts"]["due"] == 0
    assert data["today"] == TODAY.isoformat()


def test_review_lists_only_due_problems(client, root):
    a = _seed(root)
    b = _seed(root, "20. Valid Parentheses\nEasy\n\nGiven s")
    today = TODAY
    _set_due(root, a, today.isoformat(), box=3)
    _set_due(root, b, (today + timedelta(days=4)).isoformat())
    data = client.get("/review").get_json()
    assert [i["id"] for i in data["due"]] == [a]
    assert data["due"][0]["box"] == 3 and data["due"][0]["title"] == "Two Sum"
    assert data["counts"]["by_box"]["3"] == 1 and data["counts"]["by_box"]["1"] == 1
    assert data["next_due"] == (today + timedelta(days=4)).isoformat()


# --- POST /problems/<id>/grade ------------------------------------------------------

@pytest.mark.parametrize("box,grade,new_box", [
    (1, "solo", 2), (4, "solo", 5), (5, "solo", 5),
    (3, "hints", 3), (5, "peeked", 1), (2, "peeked", 1),
])
def test_grade_endpoint_applies_the_leitner_move(client, root, box, grade, new_box):
    pid = _seed(root)
    _set_due(root, pid, TODAY.isoformat(), box=box)
    resp = client.post(f"/problems/{pid}/grade", json={"grade": grade})
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    days = problem_store.REVIEW_INTERVALS[new_box - 1]
    assert data["review"]["box"] == new_box
    assert data["review"]["due"] == (TODAY + timedelta(days=days)).isoformat()
    assert data["previous"]["box"] == box
    # and the problem left today's queue
    assert client.get("/review").get_json()["counts"]["due"] == 0
    assert client.get("/review").get_json()["counts"]["reviewed_today"] == 1


def test_grade_keeps_a_validated_attempt_summary(client, root):
    pid = _seed(root)
    resp = client.post(f"/problems/{pid}/grade", json={
        "grade": "solo", "attempt": {"language": "python", "passed": 2, "total": 3}})
    assert resp.status_code == 200
    hist = problem_store.load_problem(pid, root=root)["review"]["history"]
    assert hist[-1]["attempt"] == {"language": "python", "passed": 2, "total": 3}


@pytest.mark.parametrize("body,status", [
    ({}, 400),
    ({"grade": "great"}, 400),
    ({"grade": 3}, 400),
    ({"grade": ["solo"]}, 400),
    ({"grade": "solo", "attempt": "yes"}, 400),
    ({"grade": "solo", "attempt": {"language": "ruby"}}, 400),
    ({"grade": "solo", "attempt": {"passed": -1}}, 400),
    ({"grade": "solo", "attempt": {"passed": True}}, 400),
    ({"grade": "solo", "attempt": {"passed": 3, "total": 2}}, 400),
    ({"grade": "solo", "attempt": {"total": 10**6}}, 400),
])
def test_grade_rejects_bad_input(client, root, body, status):
    pid = _seed(root)
    assert client.post(f"/problems/{pid}/grade", json=body).status_code == status
    # nothing was graded
    assert problem_store.load_problem(pid, root=root)["review"]["history"] == []


def test_grade_rejects_a_non_object_body(client, root):
    pid = _seed(root)
    assert client.post(f"/problems/{pid}/grade", json=["solo"]).status_code == 400


@pytest.mark.parametrize("pid", ["nope", "9-missing", "UPPER", "a" * 120, "x.y", "-lead"])
def test_grade_unknown_or_invalid_id_is_404(client, root, pid):
    _seed(root)
    assert client.post(f"/problems/{pid}/grade", json={"grade": "solo"}).status_code == 404


def test_grade_refuses_cross_origin(client, root):
    pid = _seed(root)
    resp = client.post(f"/problems/{pid}/grade", json={"grade": "solo"},
                       headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


# --- PUT /problems/<id>/notes ---------------------------------------------------------

def test_notes_persist_and_come_back_in_the_record(client, root):
    pid = _seed(root)
    resp = client.put(f"/problems/{pid}/notes", json={"notes": "check\r\nbefore insert"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert client.get(f"/problems/{pid}").get_json()["notes"] == "check\nbefore insert"
    # clearing works too
    assert client.put(f"/problems/{pid}/notes", json={"notes": ""}).status_code == 200
    assert client.get(f"/problems/{pid}").get_json()["notes"] == ""


def test_notes_size_cap(client, root):
    pid = _seed(root)
    ok = "x" * problem_store.NOTES_CAP
    assert client.put(f"/problems/{pid}/notes", json={"notes": ok}).status_code == 200
    too_long = ok + "y"
    assert client.put(f"/problems/{pid}/notes", json={"notes": too_long}).status_code == 400
    assert client.get(f"/problems/{pid}").get_json()["notes"] == ok


@pytest.mark.parametrize("body", [{}, {"notes": 5}, {"notes": None}, {"notes": ["a"]}])
def test_notes_reject_non_text(client, root, body):
    pid = _seed(root)
    assert client.put(f"/problems/{pid}/notes", json=body).status_code == 400


def test_notes_unknown_problem_and_cross_origin(client, root):
    assert client.put("/problems/nope/notes", json={"notes": "x"}).status_code == 404
    assert client.put("/problems/..%2Fx/notes", json={"notes": "x"}).status_code == 404
    pid = _seed(root)
    resp = client.put(f"/problems/{pid}/notes", json={"notes": "x"},
                      headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


def test_problems_listing_still_omits_notes(client, root):
    pid = _seed(root)
    client.put(f"/problems/{pid}/notes", json={"notes": "secret-ish"})
    listing = client.get("/problems").get_json()["problems"]
    assert "notes" not in listing[0]
