"""Front-end checks that need node (SP5): the zero-dependency unit tests in
``tests/js/`` and a syntax check of every first-party script. Skipped when
``node`` is not on PATH (the app itself never needs node)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


def _node(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


def test_js_unit_tests_pass():
    proc = _node("tests/js/run.js")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK - " in proc.stdout


@pytest.mark.parametrize("script", ["static/app.js", "static/lib/core.js"])
def test_first_party_scripts_parse(script):
    proc = _node("--check", script)
    assert proc.returncode == 0, proc.stderr
