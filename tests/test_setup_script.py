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
# Flat/goto-based for the same reason as the `py` stub above. Matches on the
# LITERAL "--upgrade" flag (not a bare "upgrade" substring): the requirements
# file path is under pytest's own tmp_path, which is named after the TEST
# FUNCTION -- a test named e.g. "..._pip_upgrade_failure_..." would put the
# substring "upgrade" in that path too, and a bare-substring match would
# misroute the (unrelated) `-r <path>` install call to the :upgrade branch.
_STUB_PYTHON_CMD = r"""@echo off
echo %* | findstr /I /C:"--upgrade" >nul
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

# Stub for the FAST-PATH check (`$PythonExe -c "..."`, run against an
# ALREADY-EXISTING .venv, before any pip install). Varies its answer on the
# `-c` argument's own content rather than always succeeding, so a test can
# tell apart a venv that only has `flask` installed from one that has BOTH
# `flask` AND `python-dotenv` (requirements.txt lists both; app.py imports
# `dotenv` too). If the checked import statement doesn't even mention
# `dotenv` (the OLD, buggy fast-path check - `import flask` alone), this
# always reports success, since flask-only is exactly what that narrower
# check was able to prove. Once `dotenv` IS part of the import statement (the
# fixed check), success additionally depends on FASTPATH_MISSING_DOTENV, so a
# test can simulate a partially-installed venv and prove it's rejected.
_EXISTING_VENV_PYTHON_CMD = (
    "@echo off\r\n"
    "echo %* | findstr /I /C:\"dotenv\" >nul\r\n"
    "if errorlevel 1 exit /b 0\r\n"
    "if \"%FASTPATH_MISSING_DOTENV%\"==\"1\" exit /b 1\r\n"
    "exit /b 0\r\n"
)


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
        check=False,
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


def test_pip_upgrade_failure_is_non_fatal_and_setup_still_succeeds(tmp_path):
    """#10: a failed pip SELF-upgrade must not abort setup - only warn and
    keep going with the existing pip. Blocking all of setup on this step (the
    old behavior) needlessly failed offline/flaky-network runs that would
    otherwise succeed fine with the venv's own bundled pip; only the actual
    dependency install (exercised in the next test) remains a hard failure."""
    stage, result = _run_setup(tmp_path, extra_env={"PIP_UPGRADE_FAIL": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pip" in result.stdout.lower()
    assert "warning" in result.stdout.lower()
    assert (stage / ".venv" / ".setup-ok").exists()


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


def test_existing_working_venv_skips_full_setup_without_py_launcher(tmp_path):
    """#10: if .venv's own python can already `import flask`, setup must just
    confirm it and write the marker - no `py` launcher, no pip, no network
    needed at all. This is what makes re-running the launcher work OFFLINE,
    or on a machine where the `py` launcher was never installed, once .venv
    already has a working install (e.g. the marker was lost some other way).

    PATH is restricted to just System32 + the PowerShell home dir (no
    C:\\Windows root, where the real `py` launcher lives on a machine that has
    it - see the repo's own `py.exe`) so this genuinely proves `py` is never
    invoked, not merely that it doesn't happen to be needed."""
    stage = _stage(tmp_path)
    (stage / ".venv").mkdir()  # a real pre-existing venv directory
    # A pre-existing ".venv" whose "python" is a stub that succeeds BOTH
    # `import flask` and `import flask, dotenv` (LEETCOACH_SETUP_PYTHON_EXE
    # stands in for the real `.venv\Scripts\python.exe` the same way the
    # other tests use it) - this venv genuinely has everything installed.
    python_stub = _write_stub(
        stage, "existing_venv_python.cmd", _EXISTING_VENV_PYTHON_CMD
    )

    ps_home = r"C:\Windows\System32\WindowsPowerShell\v1.0"
    env = dict(os.environ)
    env["PATH"] = r"C:\Windows\System32;" + ps_home
    env["LEETCOACH_SETUP_PYTHON_EXE"] = str(python_stub)

    result = subprocess.run(
        [
            str(Path(ps_home) / "powershell.exe"), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(stage / "setup.ps1"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
        cwd=str(stage),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (stage / ".venv" / ".setup-ok").exists()
    # Never even attempted to create a NEW venv (would show this message).
    assert "creating virtual environment" not in result.stdout.lower()


def test_existing_venv_missing_dotenv_does_not_get_the_marker(tmp_path):
    """requirements.txt lists BOTH `flask` and `python-dotenv` (app.py
    imports `dotenv`) - the fast-path check must prove BOTH are importable,
    not just `flask`. A venv with flask installed but dotenv missing must
    NOT get the marker from the fast path; it must fall through to the full
    `py`-launcher setup flow instead (which then fails here, since - like the
    sibling test above - PATH is deliberately restricted to just System32 +
    the PowerShell home dir, with no `py` launcher reachable at all)."""
    stage = _stage(tmp_path)
    (stage / ".venv").mkdir()  # a real pre-existing venv directory
    python_stub = _write_stub(
        stage, "existing_venv_python.cmd", _EXISTING_VENV_PYTHON_CMD
    )

    ps_home = r"C:\Windows\System32\WindowsPowerShell\v1.0"
    env = dict(os.environ)
    env["PATH"] = r"C:\Windows\System32;" + ps_home
    env["LEETCOACH_SETUP_PYTHON_EXE"] = str(python_stub)
    env["FASTPATH_MISSING_DOTENV"] = "1"

    result = subprocess.run(
        [
            str(Path(ps_home) / "powershell.exe"), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(stage / "setup.ps1"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
        cwd=str(stage),
    )
    # Fast path correctly refused to mark this venv OK, and fell through to
    # the full setup flow - which fails here because there's no `py`
    # launcher reachable on the restricted PATH.
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (stage / ".venv" / ".setup-ok").exists()
    assert "'py' launcher was not found" in result.stdout


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
        check=False,
        timeout=30,
        env=env,
        cwd=str(elsewhere),  # deliberately NOT the stage directory
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (stage / ".venv" / ".setup-ok").exists()
    assert not (elsewhere / ".venv").exists()
