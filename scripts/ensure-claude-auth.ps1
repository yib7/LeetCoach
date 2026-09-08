# ensure-claude-auth.ps1 — make LeetCoach's claude sign-in friction-free.
#
# Run by LeetCoach.cmd (the desktop shortcut) just before the app starts. If the
# `claude` CLI is signed out, it launches `claude auth login` right here so the
# user signs in once, then the app opens ready to run. It never blocks the app
# from starting: if sign-in can't complete, it prints the exact copy-paste
# command and lets the app open anyway (the in-app banner guides from there).

$ErrorActionPreference = "SilentlyContinue"

function Get-ClaudeLoggedIn {
    # Returns $true / $false from `claude auth status` JSON, or $null if the
    # state can't be read (binary missing, no output, unparseable).
    $text = (& claude auth status 2>$null | Out-String)
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    try { return [bool]((ConvertFrom-Json $text).loggedIn) }
    catch { return $null }
}

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "The 'claude' CLI was not found on PATH. Install Claude Code, then relaunch." -ForegroundColor Yellow
    exit 0
}

if ((Get-ClaudeLoggedIn) -eq $true) {
    exit 0  # already signed in — nothing to do
}

Write-Host ""
Write-Host "You're signed out of the 'claude' CLI — signing you in now..." -ForegroundColor Cyan
& claude auth login

if ((Get-ClaudeLoggedIn) -ne $true) {
    Write-Host ""
    Write-Host "Still signed out. To finish, run this in a terminal and complete sign-in:" -ForegroundColor Yellow
    Write-Host "    claude auth login" -ForegroundColor White
    Write-Host "LeetCoach will still open; runs work as soon as you're signed in." -ForegroundColor Yellow
}
exit 0
