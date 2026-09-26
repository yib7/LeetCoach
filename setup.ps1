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

# A marker from a PREVIOUS successful run must not survive a failed run that
# reuses the same .venv - remove it up front so a crash partway never leaves
# a stale "setup is fine" marker behind (B9).
if (Test-Path $MarkerPath) { Remove-Item $MarkerPath -Force }

# #10: if THIS .venv already has a working install (its own python can
# actually `import flask`), just confirm it and write the marker - no `py`
# launcher, no pip, no network needed. This is what makes re-running the
# shortcut work OFFLINE, or on a machine where the `py` launcher isn't
# installed at all, once .venv already has a working install from an earlier
# successful setup (e.g. the marker was lost, or setup is simply being
# re-run defensively). A broken/incomplete .venv (missing python, or import
# fails) falls through to the full install flow below exactly as before.
# Test-Path guards the call itself: with $ErrorActionPreference = "Stop",
# invoking a path that doesn't exist at all is a TERMINATING error, not just
# a nonzero exit code, and would abort the whole script instead of falling
# through on a fresh machine with no .venv yet.
if (Test-Path $PythonExe) {
    & $PythonExe -c "import flask" *> $null
    if ($LASTEXITCODE -eq 0) {
        New-Item -ItemType Directory -Force -Path $VenvPath | Out-Null
        Set-Content -Path $MarkerPath -Value (Get-Date -Format "o")
        Write-Host "Existing .venv already has the dependencies installed." -ForegroundColor Green
        Write-Host "Setup complete. To run LeetCoach:" -ForegroundColor Green
        Write-Host "  .\.venv\Scripts\Activate.ps1"
        Write-Host "  python app.py"
        exit 0
    }
}

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Exit-OnFailure "The 'py' launcher was not found. Install Python 3.12+ from https://python.org and retry."
}

# Python >= 3.12 check (B9): LeetCoach's code relies on 3.12+ stdlib behavior.
$versionText = & py -3 -c "import sys; print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))" 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($versionText)) {
    Exit-OnFailure "Could not determine the Python version from 'py -3'. Install Python 3.12+ and retry."
}
$versionParts = $versionText.Trim() -split '\.'
$pyMajor = [int]$versionParts[0]
$pyMinor = [int]$versionParts[1]
if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 12)) {
    Exit-OnFailure "Python $pyMajor.$pyMinor found via 'py -3', but LeetCoach needs Python 3.12 or newer."
}

if (-not (Test-Path $VenvPath)) {
    Write-Host "Creating virtual environment (.venv)..."
    py -3 -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        Exit-OnFailure "Failed to create the virtual environment ('py -3 -m venv' exited $LASTEXITCODE)."
    }
}

Write-Host "Installing dependencies..."
# #10: a failed pip SELF-upgrade is not fatal - warn and keep going with
# whatever pip version the venv already has. Blocking all of setup on this
# step (as before) needlessly failed offline/flaky-network runs that would
# otherwise have succeeded fine with the venv's bundled pip; the actual
# dependency install below is still a hard failure.
& $PythonExe -m pip install --upgrade pip | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Warning: failed to upgrade pip (exit code $LASTEXITCODE); continuing with the existing pip." -ForegroundColor Yellow
}

& $PythonExe -m pip install -r $RequirementsPath
if ($LASTEXITCODE -ne 0) {
    Exit-OnFailure "Failed to install dependencies (exit code $LASTEXITCODE)."
}

# Written only once every step above has actually succeeded (B9).
# LeetCoach.cmd checks for THIS marker, not just .venv's existence, before
# deciding setup doesn't need to run again.
Set-Content -Path $MarkerPath -Value (Get-Date -Format "o")

Write-Host ""
Write-Host "Setup complete. To run LeetCoach:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  python app.py"
Write-Host ""
Write-Host "Remember: the 'claude' CLI must be installed, on PATH, and authenticated."
