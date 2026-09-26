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


# #12: the path is handed to the child process via an ENV VAR, never
# interpolated into the `-Command` string. A naive `f"'{path}'"` would break
# (or worse, silently mis-parse) if the repo ever sat under a path containing
# a single quote (e.g. `C:\Users\O'Brien\...`) — `$env:...` sidesteps PowerShell
# string-quoting entirely, so the path's own content can never matter.
_PARSE_CHECK_SCRIPT = (
    "$errors = $null; "
    "[void][System.Management.Automation.Language.Parser]::ParseFile("
    "$env:LEETCOACH_TEST_PS1_PATH, [ref]$null, [ref]$errors); "
    "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 } "
    "else { exit 0 }"
)


def _parse_ps1_with_powershell(path):
    """Run `[Parser]::ParseFile` against ``path`` via a subprocess, passing the
    path through the environment (see the module comment above)."""
    env = dict(os.environ)
    env["LEETCOACH_TEST_PS1_PATH"] = str(path)
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PARSE_CHECK_SCRIPT],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pytest.mark.parametrize("path", PS1_FILES, ids=lambda p: p.name)
def test_ps1_file_parses_with_powershell_parser(path):
    """`[Parser]::ParseFile` must report zero syntax errors for every .ps1 (A1).

    This catches a broken script (bad quoting, mismatched braces, etc.) at
    test time instead of at double-click time on someone's desktop.
    """
    result = _parse_ps1_with_powershell(path)
    assert result.returncode == 0, (
        f"{path.name} failed to parse:\n{result.stdout}\n{result.stderr}"
    )


def test_ps1_parse_check_tolerates_a_single_quote_in_the_path(tmp_path):
    """#12 regression: the OLD check built the PowerShell command with
    `f"'{path}'"` — a repo checked out under a path containing an apostrophe
    (e.g. `C:\\Users\\O'Brien\\...`) would break that quoting outright. Passing
    the path via an env var instead must tolerate it cleanly. (A manually
    built path, not `tmp_path_factory.mktemp`, since mktemp sanitizes its
    basename and would strip the apostrophe we need to test.)"""
    quirky_dir = tmp_path / "o'brien's repo"
    quirky_dir.mkdir()
    script = quirky_dir / "fine.ps1"
    script.write_text("Write-Host 'hello'\n", encoding="ascii")
    result = _parse_ps1_with_powershell(script)
    assert result.returncode == 0, result.stdout + result.stderr


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


def test_bare_name_with_both_ps1_and_cmd_shims_on_path_resolves_to_the_cmd(tmp_path):
    """#2 regression: a real npm global install on Windows puts BOTH
    `claude.cmd` and `claude.ps1` on PATH under the SAME bare name.
    `Start-Process -FilePath claude` (the old code) resolves a bare name via
    Windows ShellExecute, which can pick the `.ps1` over the `.cmd` - and a
    `.ps1`'s default verb is "Edit", so Start-Process actually opened the
    script in Notepad instead of running `claude auth login`, hanging `-Wait`
    forever (reproduced empirically: identical two-shim setup + bare
    Start-Process opened Notepad on this machine). The fixed script resolves
    to a concrete Application (never an ExternalScript/.ps1) before calling
    Start-Process, so it must run the `.cmd` and never touch the `.ps1` shim
    at all, regardless of which one Windows would have picked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude.cmd").write_text(_STUB_CLAUDE_CMD, encoding="ascii")
    # A same-named .ps1 shim that answers `auth status` the SAME way the .cmd
    # does (checking the same shared marker file) so the STATUS checks (which
    # go through PowerShell's OWN `&` resolution, unaffected by #2) see a
    # consistent answer no matter which shim happens to serve them. Only
    # `auth login` differs: it writes a DIFFERENT marker, so if Start-Process
    # ever wrongly ran the `.ps1` for login (the old bug) instead of the
    # `.cmd`, the shared `logged_in.marker` would never appear and the script
    # would (wrongly) still report signed-out afterward.
    ps1_stub = (
        "$marker = Join-Path $PSScriptRoot 'logged_in.marker'\n"
        "if ($args[0] -eq 'auth' -and $args[1] -eq 'status') {\n"
        "    if (Test-Path $marker) { Write-Output '{\"loggedIn\": true}' }\n"
        "    else { Write-Output '{\"loggedIn\": false}' }\n"
        "    exit 0\n"
        "}\n"
        "if ($args[0] -eq 'auth' -and $args[1] -eq 'login') {\n"
        "    New-Item -ItemType File -Force"
        " -Path (Join-Path $PSScriptRoot 'ps1_login_ran.marker') | Out-Null\n"
        "    exit 0\n"
        "}\n"
        "exit 1\n"
    )
    (bin_dir / "claude.ps1").write_text(ps1_stub, encoding="ascii")
    env = dict(os.environ)
    env["LEETCOACH_CLAUDE_BIN"] = "claude"  # a BARE name, not a full path
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(ENSURE_CLAUDE_AUTH),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "signing you in" in result.stdout.lower()
    assert "still signed out" not in result.stdout.lower()
    assert (bin_dir / "logged_in.marker").exists()
    assert not (bin_dir / "ps1_login_ran.marker").exists()


# --- C8: CRLF pinned for .ps1/.cmd; README shows the Bypass invocation -----

def test_gitattributes_pins_ps1_and_cmd_to_crlf():
    text = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.cmd text eol=crlf" in text
    assert "*.ps1 text eol=crlf" in text


def test_tracked_launcher_scripts_are_actually_crlf_on_disk():
    """The .gitattributes rule only matters if the working tree files
    actually reflect it (a stale checkout from before the rule existed would
    still be LF) - checked at the byte level, not through a shell pipe (CRLF
    can get silently translated away in some shell pipelines)."""
    for path in PS1_FILES + CMD_FILES:
        raw = path.read_bytes()
        assert b"\r\n" in raw, f"{path.name} should be CRLF"
        # every bare \n must be part of a \r\n pair (no MIXED line endings)
        lone_lf = sum(
            1 for i, b in enumerate(raw) if b == 0x0A and raw[i - 1:i] != b"\r"
        )
        assert lone_lf == 0, f"{path.name} has {lone_lf} lone-LF line(s)"


def test_readme_shows_execution_policy_bypass_invocations():
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "powershell -ExecutionPolicy Bypass -File .\\setup.ps1" in text
    assert (
        "powershell -ExecutionPolicy Bypass -File .\\scripts\\create-shortcut.ps1" in text
    )
