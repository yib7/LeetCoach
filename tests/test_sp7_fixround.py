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
