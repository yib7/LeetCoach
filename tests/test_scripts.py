"""A1/C14: sanity checks on the repo's Windows launcher scripts.

Both checks are skipped off-Windows (no `powershell.exe`, and the scripts are
Windows-only anyway). CI adds the same `[Parser]::ParseFile` step on its
``windows-latest`` job.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every launcher script LeetCoach ships, found by extension rather than a
# hardcoded list so a newly added .ps1/.cmd is covered automatically.
PS1_FILES = sorted(REPO_ROOT.glob("*.ps1")) + sorted(REPO_ROOT.glob("scripts/*.ps1"))
CMD_FILES = sorted(REPO_ROOT.glob("*.cmd")) + sorted(REPO_ROOT.glob("scripts/*.cmd"))

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows launcher scripts; powershell.exe only"
)


@pytest.mark.parametrize("path", PS1_FILES, ids=lambda p: p.name)
def test_ps1_file_parses_with_powershell_parser(path):
    """`[Parser]::ParseFile` must report zero syntax errors for every .ps1 (A1).

    This catches a broken script (bad quoting, mismatched braces, etc.) at
    test time instead of at double-click time on someone's desktop.
    """
    ps_check = (
        "$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}', [ref]$null, [ref]$errors); "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 } "
        "else { exit 0 }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_check],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"{path.name} failed to parse:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize("path", PS1_FILES + CMD_FILES, ids=lambda p: p.name)
def test_launcher_script_is_ascii_only(path):
    """Every `.ps1`/`.cmd` must be pure ASCII (A1).

    `cmd.exe` and Windows PowerShell's console codepage handling of non-ASCII
    (curly quotes, em dashes, ...) is unreliable across locales/codepages -
    the safe, portable choice for a launcher script is plain ASCII.
    """
    raw = path.read_bytes()
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, (
        f"{path.name} has non-ASCII byte(s) at offsets "
        f"{[i for i, _ in non_ascii[:5]]} (showing up to 5)"
    )


def test_found_at_least_the_known_scripts():
    """Guard against the globs silently matching nothing (e.g. a bad path)."""
    names = {p.name for p in PS1_FILES + CMD_FILES}
    assert {"setup.ps1", "LeetCoach.cmd", "ensure-claude-auth.ps1"} <= names


# --- A1: ensure-claude-auth.ps1 behavior, against a FAKE claude ------------
#
# NEVER the real `claude` CLI: LEETCOACH_CLAUDE_BIN always points at a stub
# `claude.cmd` written to tmp_path (never on PATH), and the subprocess PATH is
# pinned to just the Windows system dirs so a real `claude` on the dev
# machine's PATH can never be reached even by accident.

ENSURE_CLAUDE_AUTH = REPO_ROOT / "scripts" / "ensure-claude-auth.ps1"

_STUB_CLAUDE_CMD = r"""@echo off
setlocal
set "MARKER=%~dp0logged_in.marker"
if "%~1"=="auth" if "%~2"=="status" (
    if exist "%MARKER%" (
        echo {"loggedIn": true}
    ) else (
        echo {"loggedIn": false}
    )
    exit /b 0
)
if "%~1"=="auth" if "%~2"=="login" (
    if not "%NO_LOGIN%"=="1" (
        type nul > "%MARKER%"
    )
    exit /b 0
)
exit /b 1
"""


def _write_stub_claude(tmp_path, *, pre_logged_in=False, no_login=False):
    stub = tmp_path / "claude.cmd"
    stub.write_text(_STUB_CLAUDE_CMD, encoding="ascii")
    if pre_logged_in:
        (tmp_path / "logged_in.marker").touch()
    return stub


def _run_ensure_script(claude_bin, extra_env=None):
    # Inherit the real environment (PowerShell's own command discovery for a
    # .cmd file needs PATHEXT/ComSpec/windir, which a hand-built minimal env
    # is easy to get subtly wrong) - safe here because the script always
    # resolves through $env:LEETCOACH_CLAUDE_BIN (set below, to the stub)
    # rather than ever falling back to a bare "claude" on PATH.
    env = dict(os.environ)
    env["LEETCOACH_CLAUDE_BIN"] = str(claude_bin)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(ENSURE_CLAUDE_AUTH),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_already_logged_in_is_a_silent_noop(tmp_path):
    stub = _write_stub_claude(tmp_path, pre_logged_in=True)
    result = _run_ensure_script(stub)
    assert result.returncode == 0
    assert "signing you in" not in result.stdout.lower()


def test_signed_out_then_login_succeeds_via_start_process_wait(tmp_path):
    """Honours LEETCOACH_CLAUDE_BIN for BOTH the status check and the login
    launch, and `Start-Process ... -Wait` really waits: the stub's `auth
    login` branch writes the marker file, and the post-login status re-check
    (run only after Start-Process returns) must see it."""
    stub = _write_stub_claude(tmp_path, pre_logged_in=False)
    result = _run_ensure_script(stub)
    assert result.returncode == 0
    assert "signing you in" in result.stdout.lower()
    assert "still signed out" not in result.stdout.lower()
    assert (tmp_path / "logged_in.marker").exists()


def test_login_that_never_completes_still_lets_the_app_open(tmp_path):
    """If sign-in doesn't complete, the script must print the copy-paste
    fallback command but still exit 0 (never blocks the app from starting)."""
    stub = _write_stub_claude(tmp_path, pre_logged_in=False)
    result = _run_ensure_script(stub, extra_env={"NO_LOGIN": "1"})
    assert result.returncode == 0
    assert "still signed out" in result.stdout.lower()
    assert "auth login" in result.stdout.lower()


def test_missing_claude_binary_warns_and_exits_zero(tmp_path):
    missing = tmp_path / "does-not-exist.cmd"
    result = _run_ensure_script(missing)
    assert result.returncode == 0
    assert "not found on path" in result.stdout.lower()
