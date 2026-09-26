"""B9: behavioral tests for LeetCoach.cmd (the desktop-shortcut launcher).

Every test stages LeetCoach.cmd into an isolated tmp_path next to FAKE
setup.ps1 / ensure-claude-auth.ps1 / .venv\\Scripts\\python.exe stand-ins, so
nothing here ever touches the real project .venv, runs the real setup, or
spawns the real `claude` CLI.

The fake "python.exe" for the app-launch tests is a tiny compiled .NET stub
(LeetCoach.cmd hardcodes a literal `.exe` path with no test-seam env var, and
Windows won't execute a same-named .cmd/.bat as a drop-in "python.exe" via
CreateProcess) - built once via `csc.exe` if available, otherwise those two
tests are skipped rather than failing an environment without it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LEETCOACH_CMD = REPO_ROOT / "LeetCoach.cmd"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows launcher script; cmd.exe only"
)

# A no-op stand-in for ensure-claude-auth.ps1: every test uses this so the
# real script (and therefore any real `claude` invocation) never runs.
_NOOP_PS1 = "exit 0\n"

# A fake setup.ps1: prints a marker line, and writes .venv\.setup-ok only when
# SETUP_SHOULD_FAIL isn't set (mirrors the real script's B9 contract enough
# for LeetCoach.cmd's own re-run/pause logic to be tested against it).
_FAKE_SETUP_PS1 = r"""
Write-Host "FAKE SETUP RAN"
New-Item -ItemType Directory -Force -Path ".venv" | Out-Null
if ($env:SETUP_SHOULD_FAIL -eq "1") {
    Write-Host "fake setup failing on purpose"
    exit 1
}
Set-Content -Path ".venv\.setup-ok" -Value "ok"
exit 0
"""


def _stage(tmp_path, *, marker_present=False):
    stage = tmp_path / "stage"
    (stage / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(LEETCOACH_CMD, stage / "LeetCoach.cmd")
    (stage / "setup.ps1").write_text(_FAKE_SETUP_PS1, encoding="utf-8")
    (stage / "scripts" / "ensure-claude-auth.ps1").write_text(_NOOP_PS1, encoding="utf-8")
    if marker_present:
        (stage / ".venv").mkdir(exist_ok=True)
        (stage / ".venv" / ".setup-ok").write_text("ok", encoding="utf-8")
    return stage


def _run(stage, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(stage / "LeetCoach.cmd")],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(stage),
        env=env,
        stdin=subprocess.DEVNULL,
    )


# --- marker-gated re-run of setup (B9) --------------------------------------

def test_marker_missing_reruns_setup(tmp_path):
    stage = _stage(tmp_path, marker_present=False)
    # let setup "succeed" but fail before reaching the (unstubbed) python.exe
    # line, so this test only needs to prove setup WAS invoked.
    result = _run(stage, extra_env={"SETUP_SHOULD_FAIL": "1"})
    assert "FAKE SETUP RAN" in result.stdout
    assert (stage / ".venv").exists()


def test_marker_present_skips_setup(tmp_path):
    stage = _stage(tmp_path, marker_present=True)
    # No usable python.exe is staged; if setup is (wrongly) skipped, the
    # script falls through to the python.exe line and fails there instead -
    # either way "FAKE SETUP RAN" must never appear.
    result = _run(stage)
    assert "FAKE SETUP RAN" not in result.stdout


def test_setup_failure_pauses_and_stops_before_launching_the_app(tmp_path):
    stage = _stage(tmp_path, marker_present=False)
    result = _run(stage, extra_env={"SETUP_SHOULD_FAIL": "1"})
    assert "Setup failed" in result.stdout
    # the marker must NOT exist (fake setup.ps1 only writes it on success)
    assert not (stage / ".venv" / ".setup-ok").exists()


# --- pause on non-zero app exit (B9) ----------------------------------------

def _csc_path():
    candidates = [
        Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
        Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"),
    ]
    return next((p for p in candidates if p.exists()), None)


@pytest.fixture(scope="module")
def fake_python_exe(tmp_path_factory):
    csc = _csc_path()
    if csc is None:
        pytest.skip("csc.exe not available to build a fake python.exe stand-in")
    build_dir = tmp_path_factory.mktemp("fake_python_build")
    source = build_dir / "prog.cs"
    source.write_text(
        "using System;\n"
        "class P {\n"
        "    static int Main() {\n"
        '        Console.WriteLine("FAKE APP RAN");\n'
        '        var codeStr = Environment.GetEnvironmentVariable("FAKE_APP_EXIT_CODE");\n'
        "        int code = 0;\n"
        "        if (!string.IsNullOrEmpty(codeStr)) { int.TryParse(codeStr, out code); }\n"
        "        return code;\n"
        "    }\n"
        "}\n",
        encoding="ascii",
    )
    out = build_dir / "python.exe"
    result = subprocess.run(
        [str(csc), "/nologo", f"/out:{out}", str(source)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0 or not out.exists():
        pytest.skip(f"could not compile fake python.exe: {result.stdout}{result.stderr}")
    return out


def _stage_with_app(tmp_path, fake_python_exe, *, app_exit_code=0):
    stage = _stage(tmp_path, marker_present=True)
    scripts_dir = stage / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(fake_python_exe, scripts_dir / "python.exe")
    (stage / "app.py").write_text("# unused by the fake python.exe\n", encoding="utf-8")
    return stage


def test_app_success_does_not_pause(tmp_path, fake_python_exe):
    stage = _stage_with_app(tmp_path, fake_python_exe, app_exit_code=0)
    result = _run(stage, extra_env={"FAKE_APP_EXIT_CODE": "0"})
    assert "FAKE APP RAN" in result.stdout
    assert "exited with an error" not in result.stdout


def test_app_nonzero_exit_pauses_with_a_message(tmp_path, fake_python_exe):
    stage = _stage_with_app(tmp_path, fake_python_exe, app_exit_code=1)
    result = _run(stage, extra_env={"FAKE_APP_EXIT_CODE": "1"})
    assert "FAKE APP RAN" in result.stdout
    assert "exited with an error" in result.stdout
