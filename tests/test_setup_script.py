"""B9: behavioral tests for setup.ps1.

NEVER a real Python/pip invocation: every test copies setup.ps1 into an
isolated tmp_path (so the real project .venv is never touched) and points
`py` at a fake stub prepended onto PATH, plus (for the pip-install steps)
LEETCOACH_SETUP_PYTHON_EXE at a second fake stub - setup.ps1 honours that env
var as a test seam instead of always hardcoding the venv's own python.exe.

Skipped off-Windows, like tests/test_scripts.py (same rationale: these are
Windows-only launcher scripts, exercised via powershell.exe).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_PS1 = REPO_ROOT / "setup.ps1"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows launcher script; powershell.exe only"
)

# Stub `py` launcher: understands just the two invocations setup.ps1 makes.
#   py -3 -c "..."          -> print a controllable fake version, exit 0
#   py -3 -m venv <path>    -> create <path>\Scripts (+ a placeholder
#                              python.exe) and exit 0, unless VENV_FAIL=1
#
# Deliberately flat (goto, not nested `if (...)` blocks): cmd.exe has a nasty,
# well-known footgun where `exit /b` inside a nested parenthesized block, with
# another statement following the nested block in the SAME outer block,
# doesn't actually abort the script - control falls through to that sibling
# statement instead and its exit code wins. goto labels sidestep it entirely.
_STUB_PY_CMD = r"""@echo off
setlocal
if "%~1"=="-3" if "%~2"=="-c" goto :version
if "%~1"=="-3" if "%~2"=="-m" if "%~3"=="venv" goto :venv
exit /b 1

:version
if "%PY_VERSION%"=="" (
    echo 3.12
) else (
    echo %PY_VERSION%
)
exit /b 0

:venv
if "%VENV_FAIL%"=="1" exit /b 1
mkdir "%~4\Scripts" >nul 2>nul
echo stub > "%~4\Scripts\python.exe"
exit /b 0
"""

# Stub "python.exe" for the pip-install steps (via LEETCOACH_SETUP_PYTHON_EXE
# so it never has to literally be a valid Win32 .exe): a plain .cmd is fine.
#   -m pip install --upgrade pip   -> exit 1 if PIP_UPGRADE_FAIL=1, else 0
#   -m pip install -r <req file>   -> exit 1 if PIP_INSTALL_FAIL=1, else 0
# Flat/goto-based for the same reason as the `py` stub above.
_STUB_PYTHON_CMD = r"""@echo off
echo %* | findstr /I "upgrade" >nul
if not errorlevel 1 goto :upgrade
echo %* | findstr /I "install" >nul
if not errorlevel 1 goto :install
exit /b 1

:upgrade
if "%PIP_UPGRADE_FAIL%"=="1" exit /b 1
exit /b 0

:install
if "%PIP_INSTALL_FAIL%"=="1" exit /b 1
exit /b 0
"""


def _stage(tmp_path):
    """Copy setup.ps1 + a throwaway requirements.txt into an isolated
    directory that stands in for the repo root, so nothing ever touches the
    real project .venv."""
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    (stage / "setup.ps1").write_text(SETUP_PS1.read_text(encoding="utf-8"), encoding="utf-8")
    (stage / "requirements.txt").write_text("flask>=3.0\n", encoding="utf-8")
    return stage


def _write_stub(dir_path, name, content):
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / name
    path.write_text(content, encoding="ascii")
    return path


def _run_setup(tmp_path, *, cwd=None, extra_env=None, python_stub=True):
    stage = _stage(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_stub(bin_dir, "py.cmd", _STUB_PY_CMD)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    if python_stub:
        python_stub_path = _write_stub(tmp_path / "pybin", "fakepython.cmd", _STUB_PYTHON_CMD)
        env["LEETCOACH_SETUP_PYTHON_EXE"] = str(python_stub_path)
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(stage / "setup.ps1"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(cwd or stage),
    )
    return stage, result


def test_successful_setup_writes_marker_and_exits_zero(tmp_path):
    stage, result = _run_setup(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "setup complete" in result.stdout.lower()
    assert (stage / ".venv" / ".setup-ok").exists()


def test_python_too_old_aborts_before_creating_a_venv(tmp_path):
    stage, result = _run_setup(tmp_path, extra_env={"PY_VERSION": "3.9"})
    assert result.returncode != 0
    assert "3.9" in result.stdout
    assert not (stage / ".venv").exists()


def test_venv_creation_failure_aborts_with_no_marker(tmp_path):
    stage, result = _run_setup(tmp_path, extra_env={"VENV_FAIL": "1"})
    assert result.returncode != 0
    assert "virtual environment" in result.stdout.lower()
    assert not (stage / ".venv" / ".setup-ok").exists()


def test_pip_upgrade_failure_aborts_with_no_marker(tmp_path):
    stage, result = _run_setup(tmp_path, extra_env={"PIP_UPGRADE_FAIL": "1"})
    assert result.returncode != 0
    assert "pip" in result.stdout.lower()
    assert not (stage / ".venv" / ".setup-ok").exists()


def test_pip_install_failure_aborts_with_no_marker(tmp_path):
    stage, result = _run_setup(tmp_path, extra_env={"PIP_INSTALL_FAIL": "1"})
    assert result.returncode != 0
    assert "depend" in result.stdout.lower()
    assert not (stage / ".venv" / ".setup-ok").exists()


def test_stale_marker_from_a_prior_run_is_removed_on_failure(tmp_path):
    """A marker left by an earlier SUCCESSFUL run must not survive a run that
    reuses the same .venv and then fails (B9) - otherwise LeetCoach.cmd would
    wrongly treat a broken venv as already set up."""
    stage, result = _run_setup(tmp_path)
    assert result.returncode == 0
    marker = stage / ".venv" / ".setup-ok"
    assert marker.exists()

    _, result2 = _run_setup(
        tmp_path, cwd=stage, extra_env={"PIP_INSTALL_FAIL": "1"},
    )
    # this reruns against the SAME staged setup.ps1/.venv from the first call
    assert result2.returncode != 0
    assert not marker.exists()


def test_setup_is_dollar_psscriptroot_relative_not_cwd_relative(tmp_path):
    """Running setup.ps1 via a full path from an unrelated working directory
    must still create .venv NEXT TO THE SCRIPT, not in the caller's cwd (B9)."""
    stage = _stage(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    bin_dir = tmp_path / "bin"
    _write_stub(bin_dir, "py.cmd", _STUB_PY_CMD)
    python_stub_path = _write_stub(tmp_path / "pybin", "fakepython.cmd", _STUB_PYTHON_CMD)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["LEETCOACH_SETUP_PYTHON_EXE"] = str(python_stub_path)

    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(stage / "setup.ps1"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(elsewhere),  # deliberately NOT the stage directory
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (stage / ".venv" / ".setup-ok").exists()
    assert not (elsewhere / ".venv").exists()
