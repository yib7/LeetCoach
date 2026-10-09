# One-command setup for LeetCoach on Windows.  Run:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Creates a local .venv and installs the runtime dependencies.
$ErrorActionPreference = "Stop"

# Resolve every path relative to THIS SCRIPT's own directory, not the caller's
# current working directory (B9) - so `setup.ps1` behaves the same whether
# it's run as `.\setup.ps1`, via a full path, or from LeetCoach.cmd.
Set-Location -Path $PSScriptRoot

$VenvPath = Join-Path $PSScriptRoot ".venv"
$MarkerPath = Join-Path $VenvPath ".setup-ok"
$RequirementsPath = Join-Path $PSScriptRoot "requirements.txt"

# Test seam only: lets tests point pip installs at a fake stub instead of a
# real interpreter, without a real `.venv` ever being created. Unset (the
# normal case), this is exactly `.venv\Scripts\python.exe`.
$PythonExe = $env:LEETCOACH_SETUP_PYTHON_EXE
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = Join-Path $VenvPath "Scripts\python.exe"
}

function Exit-OnFailure {
    param([string]$Message)
    Write-Host ""
    Write-Host $Message -ForegroundColor Red
    exit 1
}

# Shared by both the fast path and the full setup path below, so the two
# don't drift out of sync with each other.
function Write-SetupCompleteMessage {
    Write-Host "Setup complete. To run LeetCoach:" -ForegroundColor Green
    Write-Host "  .\.venv\Scripts\Activate.ps1"
    Write-Host "  python app.py"
}

# 3A S1: every native command (python, py, pip) runs through these two
# helpers. Under Windows PowerShell 5.1 with $ErrorActionPreference = "Stop",
# a native command's stderr that is redirected (2>$null, *> $null, 2>&1)
# turns its FIRST stderr line into a terminating NativeCommandError - so a
# venv python printing "ModuleNotFoundError", or `py -3` printing "No
# suitable Python runtime found", aborted the whole script instead of
# reaching the exit-code check after it. Each helper sets "Continue" in its
# OWN scope only (the script keeps "Stop" for its cmdlets), judges the
# command purely by its exit code, and reports a command that could not be
# started at all (a missing or non-executable path) as exit code 1.
function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [switch]$Quiet,
        [switch]$HideOutput
    )
    $ErrorActionPreference = "Continue"
    try {
        if ($Quiet) {
            & $FilePath @Arguments *> $null
        } elseif ($HideOutput) {
            & $FilePath @Arguments | Out-Null
        } else {
            & $FilePath @Arguments | Out-Host
        }
        return $LASTEXITCODE
    } catch {
        if (-not $Quiet) { Write-Host $_.Exception.Message -ForegroundColor Yellow }
        return 1
    }
}

# Like Invoke-Native, but captures stdout (stderr discarded) for a probe.
function Get-NativeOutput {
    param([string]$FilePath, [string[]]$Arguments)
    $ErrorActionPreference = "Continue"
    try {
        $out = & $FilePath @Arguments 2>$null
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($out | Out-String) }
    } catch {
        return [pscustomobject]@{ ExitCode = 1; Output = "" }
    }
}

# A marker from a PREVIOUS successful run must not survive a failed run that
# reuses the same .venv - remove it up front so a crash partway never leaves
# a stale "setup is fine" marker behind (B9).
if (Test-Path $MarkerPath) { Remove-Item $MarkerPath -Force }

# #10: if THIS .venv already has a working install (its own python can
# actually import every runtime dependency - flask AND python-dotenv; app.py
# imports both), just confirm it and write the marker - no `py` launcher, no
# pip, no network needed. This is what makes re-running the shortcut work
# OFFLINE, or on a machine where the `py` launcher isn't installed at all,
# once .venv already has a working install from an earlier successful setup
# (e.g. the marker was lost, or setup is simply being re-run defensively).
# Test-Path guards the call so a fresh machine with no .venv yet goes
# straight on to the full install.
#
# 3A S1: anything else decides how the full install below starts. A venv
# whose python RUNS (`import sys` works) but lacks a dependency (e.g. only
# flask got installed before a previous run was interrupted) is reused and
# pip-installed into. A venv whose python is MISSING (an interrupted
# creation, a half-deleted folder) or cannot even start (its base
# interpreter was uninstalled) is recreated from scratch (`venv --clear`) -
# installing into it, or running a path that isn't there, can never work.
$VenvUsable = $false
if (Test-Path $PythonExe) {
    if ((Invoke-Native -FilePath $PythonExe -Arguments @("-c", "import flask, dotenv") -Quiet) -eq 0) {
        New-Item -ItemType Directory -Force -Path $VenvPath | Out-Null
        Set-Content -Path $MarkerPath -Value (Get-Date -Format "o")
        Write-Host "Existing .venv already has the dependencies installed." -ForegroundColor Green
        Write-SetupCompleteMessage
        exit 0
    }
    $VenvUsable = (Test-Path $VenvPath) -and
        ((Invoke-Native -FilePath $PythonExe -Arguments @("-c", "import sys") -Quiet) -eq 0)
}

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Exit-OnFailure "The 'py' launcher was not found. Install Python 3.12+ from https://python.org and retry."
}

# Python >= 3.12 check (B9): LeetCoach's code relies on 3.12+ stdlib behavior.
$versionProbe = Get-NativeOutput -FilePath "py" -Arguments @("-3", "-c", "import sys; print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))")
$versionText = $versionProbe.Output
if ($versionProbe.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($versionText)) {
    Exit-OnFailure "Could not determine the Python version from 'py -3'. Install Python 3.12+ and retry."
}
$versionParts = $versionText.Trim() -split '\.'
$pyMajor = [int]$versionParts[0]
$pyMinor = [int]$versionParts[1]
if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 12)) {
    Exit-OnFailure "Python $pyMajor.$pyMinor found via 'py -3', but LeetCoach needs Python 3.12 or newer."
}

if (-not $VenvUsable) {
    if (Test-Path $VenvPath) {
        Write-Host "Recreating the virtual environment (.venv) - its Python is missing or cannot run..."
        $venvExit = Invoke-Native -FilePath "py" -Arguments @("-3", "-m", "venv", "--clear", $VenvPath)
    } else {
        Write-Host "Creating virtual environment (.venv)..."
        $venvExit = Invoke-Native -FilePath "py" -Arguments @("-3", "-m", "venv", $VenvPath)
    }
    if ($venvExit -ne 0) {
        Exit-OnFailure "Failed to create the virtual environment ('py -3 -m venv' exited $venvExit)."
    }
    if (-not (Test-Path $PythonExe)) {
        Exit-OnFailure "The virtual environment was created, but $PythonExe is missing. Delete the .venv folder and retry."
    }
}

Write-Host "Installing dependencies..."
# #10: a failed pip SELF-upgrade is not fatal - warn and keep going with
# whatever pip version the venv already has. Blocking all of setup on this
# step (as before) needlessly failed offline/flaky-network runs that would
# otherwise have succeeded fine with the venv's bundled pip; the actual
# dependency install below is still a hard failure.
$pipExit = Invoke-Native -FilePath $PythonExe -Arguments @("-m", "pip", "install", "--upgrade", "pip") -HideOutput
if ($pipExit -ne 0) {
    Write-Host "Warning: failed to upgrade pip (exit code $pipExit); continuing with the existing pip." -ForegroundColor Yellow
}

$installExit = Invoke-Native -FilePath $PythonExe -Arguments @("-m", "pip", "install", "-r", $RequirementsPath)
if ($installExit -ne 0) {
    Exit-OnFailure "Failed to install dependencies (exit code $installExit)."
}

# Written only once every step above has actually succeeded (B9).
# LeetCoach.cmd checks for THIS marker, not just .venv's existence, before
# deciding setup doesn't need to run again.
Set-Content -Path $MarkerPath -Value (Get-Date -Format "o")

Write-Host ""
Write-SetupCompleteMessage
Write-Host ""
Write-Host "Remember: the 'claude' CLI must be installed, on PATH, and authenticated."
