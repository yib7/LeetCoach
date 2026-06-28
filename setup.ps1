# One-command setup for LeetCoach on Windows.  Run:  .\setup.ps1
# Creates a local .venv and installs the runtime dependencies.
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment (.venv)..."
    py -m venv .venv
}

Write-Host "Installing dependencies..."
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host ""
Write-Host "Setup complete. To run LeetCoach:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  python app.py"
Write-Host ""
Write-Host "Remember: the 'claude' CLI must be installed, on PATH, and authenticated."
