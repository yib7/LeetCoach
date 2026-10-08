"""SP7 fix round: spreadsheet-safe Anki TSV fields (3), a cancelled "Test my
code" run frees its slot for the next one (6), record merges keep the review
schedule and cap notes (7), and attempt stderr without the sandbox's
throwaway run-dir path (9). Every Claude call is a fake."""
from __future__ import annotations

import csv
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

import app as app_module
import claude_cli
import flashcards as fc
import practice
import problem_store as ps

# --- 3: formula injection in the TSV export -------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("=1+2", "'=1+2"),
    ("+cmd", "'+cmd"),
    ("-1", "'-1"),
    ("@SUM(A1)", "'@SUM(A1)"),
    ("\tlead tab", "\"'\tlead tab\""),
    ("\rlead cr", "\"'\nlead cr\""),
    ('=HYPERLINK("x")', "\"'=HYPERLINK(\"\"x\"\")\""),
])
def test_tsv_field_neutralizes_formula_starts(raw, expected):
    assert fc.tsv_field(raw) == expected


def test_tsv_field_leaves_safe_text_alone():
    assert fc.tsv_field("a = b + c") == "a = b + c"
    assert fc.tsv_field("O(n) - linear") == "O(n) - linear"
    assert fc.tsv_field("#not a comment") == '"#not a comment"'


def test_tsv_export_keeps_its_header_and_neutralizes_cells():
    text = fc.to_tsv([{"q": "=2+3", "a": "-1", "pattern": "math", "problem_id": "1-x"}])
    head, _, body = text.partition("#tags column:3\n")
    assert head == "#separator:tab\n#html:false\n#columns:Front\tBack\tTags\n"
    rows = list(csv.reader(io.StringIO(body), delimiter="\t"))
    assert rows == [["'=2+3", "'-1", "leetcoach math 1-x"]]


# --- 9: stderr without the sandbox run-dir path ----------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ('  File "C:\\Users\\Jo Smith\\AppData\\Local\\Temp\\leetcoach_run_0knjqxj5\\solution.py", '
     'line 2', '  File "solution.py", line 2'),
    ('  File "/tmp/leetcoach_run_ab_12/solution.py", line 7, in <module>',
     '  File "solution.py", line 7, in <module>'),
    ("can't open file 'C:\\Temp\\leetcoach_run_x1\\solution.py'", "can't open file 'solution.py'"),
    ("a/b and C:\\T\\leetcoach_run_q\\solution.py", "a/b and solution.py"),
    ("ValueError: boom", "ValueError: boom"),
])
def test_strip_run_dir(raw, expected):
    assert practice.strip_run_dir(raw) == expected


def test_run_cases_strips_the_run_dir_from_stderr_and_notes():
    trace = ('Traceback (most recent call last):\n'
             '  File "C:\\Temp\\leetcoach_run_0knjqxj5\\solution.py", line 2, in <module>\n'
             "ValueError: boom")

    def verify(code, stdin, expected, **kw):
        return SimpleNamespace(status="error", note="exited with code 1",
                               detail=[{"stdout": "", "stderr": trace, "returncode": 1}])

    out = practice.run_cases("x", [practice.Case("sample", 1, "a\n", "b")], timeout=1,
                             verify=verify)
    assert out["cases"][0]["stderr"] == (
        'Traceback (most recent call last):\n'
        '  File "solution.py", line 2, in <module>\nValueError: boom')


# --- 6: a cancelled test frees the slot promptly ---------------------------------------

PASTE = (
    "1. Two Sum\nEasy\n\nGiven nums.\n\n"
    "Example 1:\nInput: nums = [2,7,11,15], target = 9\nOutput: [0,1]\n"
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(out))
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


def _seed(root, paste=PASTE):
    return ps.record_run(
        paste, mode="answer", language="python", tier="normal", model="m", verdict="pass",
        paths=[root / "answers/hash_map/two_sum__normal.md"], session_id=None,
        duration_s=1.0, pattern="hash_map", root=root)


def _body(pid, test_id):
    return {"problem_id": pid, "code": "print(1)", "language": "python", "test_id": test_id}


def test_a_new_test_waits_for_a_cancelled_run_instead_of_409(application, root, monkeypatch):
    pid = _seed(root)
    started = threading.Event()

    def slow_run_cases(code, cases, *, problem_text="", cancel=None, **kw):
        if started.is_set():  # the second run: instant
            return {"status": "pass", "cases": []}
        started.set()
        cancel.wait(10)
        time.sleep(0.4)  # the sandbox takes a moment to kill and clean up
        return {"status": "not_verified", "cases": []}

    monkeypatch.setattr(practice, "run_cases", slow_run_cases)
    box = {}

    def first():
        with application.test_client() as c:
            box["resp"] = c.post("/attempt/test", json=_body(pid, "t-1"))

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(5)
    client = application.test_client()
    assert client.post("/attempt/cancel", json={"test_id": "t-1"}).get_json() == {
        "cancelled": True}
    resp = client.post("/attempt/test", json=_body(pid, "t-2"))
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["status"] == "pass"
    t.join(10)
    assert box["resp"].get_json()["cancelled"] is True


def test_a_running_uncancelled_test_still_gives_409(application, root, monkeypatch):
    pid = _seed(root)
    started, release = threading.Event(), threading.Event()

    def slow_run_cases(code, cases, *, problem_text="", cancel=None, **kw):
        started.set()
        release.wait(10)
        return {"status": "pass", "cases": []}

    monkeypatch.setattr(practice, "run_cases", slow_run_cases)
    t = threading.Thread(target=lambda: application.test_client().post(
        "/attempt/test", json=_body(pid, "t-1")))
    t.start()
    assert started.wait(5)
    t0 = time.monotonic()
    resp = application.test_client().post("/attempt/test", json=_body(pid, "t-2"))
    assert resp.status_code == 409
    assert time.monotonic() - t0 < 1  # no waiting on a run nobody cancelled
    release.set()
    t.join(10)
