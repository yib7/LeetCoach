# ensure-claude-auth.ps1 - make LeetCoach's claude sign-in friction-free.
#
# Run by LeetCoach.cmd (the desktop shortcut) just before the app starts. If the
# `claude` CLI is signed out, it launches `claude auth login` in its own visible
# window (A1 - the desktop shortcut's console runs minimized, see
# scripts/create-shortcut.ps1, so a login prompt run directly in THIS console
# would be invisible to the user) so the user signs in once, then the app opens
# ready to run. It never blocks the app from starting: if sign-in can't
# complete, it prints the exact copy-paste command and lets the app open
# anyway (the in-app banner guides from there).

$ErrorActionPreference = "SilentlyContinue"

# Honour LEETCOACH_CLAUDE_BIN so a non-PATH `claude` install is found the same
# way the Python app resolves it (config.claude_bin()).
$ClaudeBin = $env:LEETCOACH_CLAUDE_BIN
if ([string]::IsNullOrWhiteSpace($ClaudeBin)) { $ClaudeBin = "claude" }

function Get-ClaudeLoggedIn {
    # Returns $true / $false from `claude auth status` JSON, or $null if the
    # state can't be read (binary missing, no output, unparseable).
    $text = (& $ClaudeBin auth status 2>$null | Out-String)
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    try { return [bool]((ConvertFrom-Json $text).loggedIn) }
    catch { return $null }
}

if (-not (Get-Command $ClaudeBin -ErrorAction SilentlyContinue)) {
    Write-Host "The 'claude' CLI was not found on PATH. Install Claude Code, then relaunch." -ForegroundColor Yellow
    exit 0
}

if ((Get-ClaudeLoggedIn) -eq $true) {
    exit 0  # already signed in - nothing to do
}

Write-Host ""
Write-Host "You're signed out of the 'claude' CLI - signing you in now..." -ForegroundColor Cyan
# A1: run login in its own normal (non-minimized) window and wait for it,
# rather than inline in this console - see the header comment above.
Start-Process -FilePath $ClaudeBin -ArgumentList "auth", "login" -WindowStyle Normal -Wait

if ((Get-ClaudeLoggedIn) -ne $true) {
    Write-Host ""
    Write-Host "Still signed out. To finish, run this in a terminal and complete sign-in:" -ForegroundColor Yellow
    Write-Host "    $ClaudeBin auth login" -ForegroundColor White
    Write-Host "LeetCoach will still open; runs work as soon as you're signed in." -ForegroundColor Yellow
}
exit 0
