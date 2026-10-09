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
    (('  File "C:\\Users\\Jo Smith\\AppData\\Local\\Temp\\leetcoach_run_0knjqxj5\\solution.py", '
      'line 2'), '  File "solution.py", line 2'),
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


# --- 7: merging an un-numbered record into a numbered one ------------------------------

def _write(root, pid, **fields):
    folder = root / ".leetcoach" / "problems"
    folder.mkdir(parents=True, exist_ok=True)
    rec = {"id": pid, "number": None, "title": "Two Sum", "difficulty": None,
           "pattern": "hash_map", "statement": "", "created": "2026-01-01T00:00:00",
           "updated": "2026-01-01T00:00:00", "notes": "",
           "review": {"box": 1, "due": "2026-01-02", "history": []},
           "runs": [], "aliases": []}
    rec.update(fields)
    (folder / f"{pid}.json").write_text(json.dumps(rec), "utf-8")


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


# --- 3A W3: GET /problems keeps only string aliases from a hand-edited record ------

@pytest.mark.parametrize("raw,expected", [
    ([{"x": 1}, "two_sum", 5, ["y"]], ["two_sum"]),
    ("two_sum", []),
    ({"two_sum": 1}, []),
    (7, []),
])
def test_problems_listing_filters_non_string_aliases(root, application, raw, expected):
    _write(root, "1-two_sum", number=1, aliases=raw)
    resp = application.test_client().get("/problems")
    assert resp.status_code == 200
    (item,) = resp.get_json()["problems"]
    assert item["aliases"] == expected
    assert item["run_count"] == 0
