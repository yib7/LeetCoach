"""SP7 / D3: ``POST /attempt/test`` ("Test my code" in the re-attempt view)
and the ``practice`` helpers. The happy paths run the REAL sandbox
(``sandbox.verify_python``: local, already used across the suite); no
Claude call is ever made."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import app as app_module
import claude_cli
import practice
import problem_store

PASTE = (
    "1. Two Sum\nEasy\n\nGiven an array of integers nums and an integer target, return the "
    "indices of the two numbers that add up to target.\n\n"
    "Example 1:\nInput: nums = [2,7,11,15], target = 9\nOutput: [0,1]\n\n"
    "Example 2:\nInput: nums = [3,2,4], target = 6\nOutput: [1,2]\n"
)

GOOD = '''\
import ast, json, re, sys

def two_sum(nums, target):
    seen = {}
    for i, x in enumerate(nums):
        if target - x in seen:
            return [seen[target - x], i]
        seen[x] = i
    return []

text = sys.stdin.read()
args = {k: ast.literal_eval(v) for k, v in re.findall(r"(\\w+)\\s*=\\s*(\\[[^\\]]*\\]|-?\\d+)", text)}
print(json.dumps(two_sum(args["nums"], args["target"]), separators=(",", ":")))
'''
WRONG = GOOD.replace("return [seen[target - x], i]", "return [i, seen[target - x]]")
CRASH = "import sys\nsys.stdin.read()\nraise ValueError('boom')\n"
ECHO = "import sys\nprint(sys.stdin.read().strip().upper())\n"


@pytest.fixture
def root(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(out))
    monkeypatch.setenv("LEETCOACH_VERIFY_TIMEOUT", "10")
    return out


@pytest.fixture
def application(root):
    def no_claude(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("no Claude call expected")

    application = app_module.create_app(
        run_fn=no_claude,
        auth_probe=lambda: claude_cli.AuthStatus(installed=True, logged_in=True))
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(application):
    return application.test_client()


def _seed(root, paste=PASTE):
    return problem_store.record_run(
        paste, mode="answer", language="python", tier="normal", model="m", verdict="pass",
        paths=[root / "answers/hash_map/two_sum__normal.md"], session_id=None,
        duration_s=1.0, pattern="hash_map", root=root)


def _test(client, pid, code, **extra):
    body = {"problem_id": pid, "code": code, "language": "python"}
    body.update(extra)
    return client.post("/attempt/test", json=body)


# --- the real sandbox, end to end --------------------------------------------------

def test_passing_code_passes_every_sample_and_custom_case(client, root):
    pid = _seed(root)
    resp = _test(client, pid, GOOD, cases=[
        {"input": "nums = [1,5,9], target = 14", "expected": "[1,2]"}])
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["supported"] is True and data["status"] == "pass"
    assert (data["passed"], data["total"], data["ran"]) == (3, 3, 3)
    assert [(c["source"], c["index"], c["status"]) for c in data["cases"]] == [
        ("sample", 1, "pass"), ("sample", 2, "pass"), ("custom", 1, "pass")]
    assert data["cases"][0]["input"] == "nums = [2,7,11,15], target = 9"
    assert data["cases"][0]["expected"] == "[0,1]"
    assert data["cases"][0]["got"].strip() == "[0,1]"
    assert data["summary"] == "3/3 passed"


def test_failing_code_reports_each_differing_case(client, root):
    pid = _seed(root)
    data = _test(client, pid, WRONG).get_json()
    assert data["status"] == "fail"
    assert [c["status"] for c in data["cases"]] == ["fail", "fail"]
    assert data["cases"][0]["got"].strip() == "[1,0]"
    assert data["passed"] == 0 and data["total"] == 2


def test_a_custom_case_can_fail_while_samples_pass(client, root):
    pid = _seed(root)
    data = _test(client, pid, GOOD, cases=[
        {"input": "nums = [1,2], target = 3", "expected": "[1,0]"}]).get_json()
    assert data["status"] == "fail"
    assert [c["status"] for c in data["cases"]] == ["pass", "pass", "fail"]
    assert data["summary"] == "2/3 passed"


def test_erroring_code_reports_the_crash(client, root):
    pid = _seed(root)
    data = _test(client, pid, CRASH).get_json()
    assert data["status"] == "error"
    case = data["cases"][0]
    assert case["status"] == "error"
    assert "ValueError" in case["stderr"] and "boom" in case["stderr"]
    # SP7 fix 9: the traceback names solution.py, not the throwaway run dir
    assert 'File "solution.py"' in case["stderr"]
    assert "leetcoach_run_" not in case["stderr"]
    assert case["returncode"] not in (0, None)
    assert "exited with code" in case["note"]


def test_custom_case_without_expected_just_runs(client, root):
    pid = _seed(root)
    data = _test(client, pid, ECHO, include_samples=False,
                 cases=[{"input": "hello there"}, {"input": "x", "expected": ""}]).get_json()
    assert data["status"] == "ran"
    assert [c["status"] for c in data["cases"]] == ["ran", "ran"]
    assert data["cases"][0]["got"].strip() == "HELLO THERE"
    assert data["cases"][0]["expected"] is None
    assert data["total"] == 0 and data["ran"] == 2


def test_custom_only_when_the_statement_has_no_samples(client, root):
    pid = _seed(root, "7. Reverse Integer\nMedium\n\nReverse the digits of x.")
    resp = _test(client, pid, ECHO)
    assert resp.status_code == 400
    assert "add a case" in resp.get_json()["error"]
    data = _test(client, pid, ECHO, cases=[{"input": "ab", "expected": "AB"}]).get_json()
    assert data["status"] == "pass" and data["cases"][0]["source"] == "custom"


def test_crlf_custom_input_is_normalized(client, root):
    pid = _seed(root)
    data = _test(client, pid, ECHO, include_samples=False,
                 cases=[{"input": "a\r\nb\r\n", "expected": "A\r\nB"}]).get_json()
    assert data["status"] == "pass"


# --- non-Python + validation -----------------------------------------------------------

@pytest.mark.parametrize("language,label", [("cpp", "C++"), ("java", "Java")])
def test_cpp_and_java_are_not_supported_yet(client, root, language, label):
    pid = _seed(root)
    resp = client.post("/attempt/test", json={
        "problem_id": pid, "code": "int main(){}", "language": language})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["supported"] is False and data["status"] == "not_supported"
    assert f"Running {label} code is not supported yet" in data["message"]


@pytest.mark.parametrize("body,status", [
    ({"code": GOOD, "language": "python"}, 400),                          # no id
    ({"problem_id": "../x", "code": GOOD, "language": "python"}, 400),
    ({"problem_id": 5, "code": GOOD, "language": "python"}, 400),
    ({"problem_id": "9-missing", "code": GOOD, "language": "python"}, 404),
    ({"problem_id": "PID", "code": GOOD, "language": "ruby"}, 400),
    ({"problem_id": "PID", "code": 7, "language": "python"}, 400),
    ({"problem_id": "PID", "code": "   ", "language": "python"}, 400),
    ({"problem_id": "PID", "code": "x" * (practice.CODE_CAP + 1), "language": "python"}, 400),
    ({"problem_id": "PID", "code": GOOD, "language": "python", "cases": "x"}, 400),
    ({"problem_id": "PID", "code": GOOD, "language": "python", "cases": [5]}, 400),
    ({"problem_id": "PID", "code": GOOD, "language": "python",
      "cases": [{"input": 1, "expected": "2"}]}, 400),
    ({"problem_id": "PID", "code": GOOD, "language": "python",
      "cases": [{"input": "  ", "expected": "2"}]}, 400),
    ({"problem_id": "PID", "code": GOOD, "language": "python",
      "cases": [{"input": "x" * (practice.CASE_FIELD_CAP + 1)}]}, 400),
    ({"problem_id": "PID", "code": GOOD, "language": "python",
      "cases": [{"input": "a"}] * (practice.MAX_CUSTOM_CASES + 1)}, 400),
    ({"problem_id": "PID", "code": GOOD, "language": "python", "include_samples": "no"}, 400),
    ({"problem_id": "PID", "code": GOOD, "language": "python", "test_id": "bad id!"}, 400),
])
def test_attempt_validates_input(client, root, monkeypatch, body, status):
    pid = _seed(root)
    calls = []
    monkeypatch.setattr(practice, "run_cases", lambda *a, **k: calls.append(1) or {})
    body = {k: (pid if v == "PID" else v) for k, v in body.items()}
    resp = client.post("/attempt/test", json=body)
    assert resp.status_code == status, resp.get_json()
    assert calls == []  # nothing ran


def test_attempt_rejects_non_object_and_cross_site(client, root):
    pid = _seed(root)
    assert client.post("/attempt/test", json=[pid]).status_code == 400
    resp = client.post("/attempt/test", json={"problem_id": pid, "code": GOOD,
                                              "language": "python"},
                       headers={"Origin": "http://localhost:9999"})
    assert resp.status_code == 403


# --- one run at a time + cancel ---------------------------------------------------------

def test_second_concurrent_test_is_409_and_cancel_stops_the_first(application, root, monkeypatch):
    pid = _seed(root)
    started = threading.Event()
    seen_cancel = threading.Event()

    def slow_verify(code, stdin, expected, *, timeout, problem_text, cancel):
        started.set()
        if cancel.wait(10):
            seen_cancel.set()
            return SimpleNamespace(status="not_verified", note="verification cancelled", detail=[])
        return SimpleNamespace(status="pass", note="", detail=[{"stdout": expected}])

    real_run_cases = practice.run_cases
    monkeypatch.setattr(practice, "run_cases",
                        lambda *a, **k: real_run_cases(*a, verify=slow_verify, **k))
    box = {}

    def first():
        with application.test_client() as c:
            box["resp"] = _test(c, pid, GOOD, test_id="t-1")

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(5)
    client = application.test_client()
    assert _test(client, pid, GOOD, test_id="t-2").status_code == 409
    assert client.post("/attempt/cancel", json={"test_id": "nope"}).status_code == 404
    assert client.post("/attempt/cancel", json={"test_id": "bad id"}).status_code == 400
    resp = client.post("/attempt/cancel", json={"test_id": "t-1"})
    assert resp.get_json() == {"cancelled": True}
    t.join(10)
    assert seen_cancel.is_set()
    data = box["resp"].get_json()
    assert data["cancelled"] is True and data["status"] == "not_verified"
    assert data["skipped"] == 2 and "not run" in data["summary"]
    # the slot is free again
    monkeypatch.setattr(practice, "run_cases", lambda *a, **k: {"status": "pass"})
    assert _test(client, pid, GOOD).status_code == 200


# --- practice.run_cases aggregation (no sandbox) ----------------------------------------

def _fake(results):
    it = iter(results)

    def verify(code, stdin, expected, **kw):
        status, out = next(it)
        return SimpleNamespace(status=status, note=status, detail=[{"stdout": out, "stderr": ""}])
    return verify


C = practice.Case


@pytest.mark.parametrize("results,expect", [
    ([("pass", "1"), ("pass", "2")], "pass"),
    ([("pass", "1"), ("fail", "x")], "fail"),
    ([("error", ""), ("error", "")], "error"),
    ([("pass", "1"), ("error", "")], "fail"),
    ([("not_verified", "")], "not_verified"),
])
def test_aggregate_status(results, expect):
    cases = [C("sample", i, "in\n", "1") for i in range(1, len(results) + 1)]
    out = practice.run_cases("x", cases, verify=_fake(results), timeout=1)
    assert out["status"] == expect


def test_not_verified_midway_skips_the_rest():
    cases = [C("sample", i, "in\n", "1") for i in range(1, 4)]
    out = practice.run_cases("x", cases, timeout=1,
                             verify=_fake([("pass", "1"), ("not_verified", "")]))
    assert out["ran"] == 1 and out["skipped"] == 2 and out["status"] == "pass"
    assert "2 not run" in out["summary"]


def test_echoed_fields_are_clipped():
    big = "y" * (practice.ECHO_CAP + 50)
    out = practice.run_cases("x", [C("custom", 1, "in\n", None)], timeout=1,
                             verify=_fake([("fail", big)]))
    assert out["cases"][0]["got"].endswith("(truncated)")
    assert len(out["cases"][0]["got"]) < practice.ECHO_CAP + 20


def test_sample_cases_come_from_the_statement():
    cases = practice.sample_cases(PASTE)
    assert [(c.stdin, c.expected) for c in cases] == [
        ("nums = [2,7,11,15], target = 9\n", "[0,1]"), ("nums = [3,2,4], target = 6\n", "[1,2]")]


def test_run_cases_honours_a_preset_cancel():
    ev = threading.Event()
    ev.set()
    start = time.monotonic()
    out = practice.run_cases("x", [C("sample", 1, "a\n", "b")], cancel=ev, timeout=1,
                             verify=_fake([("pass", "b")]))
    assert out["status"] == "not_verified" and out["skipped"] == 1
    assert time.monotonic() - start < 1
