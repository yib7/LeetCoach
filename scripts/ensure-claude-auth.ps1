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
    # state can't be read (binary missing, no output, unparseable). The `&`
    # call operator uses PowerShell's OWN command resolution (Get-Command),
    # which runs an ExternalScript (.ps1) or a native Application correctly
    # either way - unlike Start-Process below, this is not the buggy path.
    $text = (& $ClaudeBin auth status 2>$null | Out-String)
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    try { return [bool]((ConvertFrom-Json $text).loggedIn) }
    catch { return $null }
}

function Start-ClaudeLogin {
    # Start-Process's bare-name resolution goes through Windows' ShellExecute
    # (OS file-association lookup), NOT PowerShell's Get-Command. When both a
    # `claude.cmd` and a `claude.ps1` sit on PATH (a real npm global install on
    # Windows ships both), ShellExecute can pick the `.ps1` - and a `.ps1`'s
    # default verb is "Edit", so Start-Process actually opens the script in
    # Notepad instead of running it, and `-Wait` then hangs forever waiting
    # for Notepad to close (reproduced on this machine: identical repro with
    # two same-named shims). Resolve to a concrete Application (.exe/.com/
    # .bat/.cmd - Get-Command's "Application" type never includes an
    # ExternalScript/.ps1) FIRST so Start-Process is always handed something
    # it can actually execute.
    $resolved = Get-Command $ClaudeBin -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($resolved) {
        return Start-Process -FilePath $resolved.Source -ArgumentList "auth", "login" -WindowStyle Normal -PassThru
    }
    # No native Application under this name (e.g. Get-Command found nothing
    # at all, or only a .ps1 shim, for which the "Application" CommandType
    # never matches) - launch through the command processor instead of
    # ShellExecute. This sidesteps the Edit-verb/Notepad-hang problem, since
    # cmd.exe never invokes ShellExecute's file-association lookup - but it
    # does NOT make a .ps1-only shim runnable: cmd.exe's own PATHEXT-based
    # resolution never considers .ps1 either, so that case still just fails
    # fast here with a normal "not recognized" error instead of hanging.
    return Start-Process -FilePath $env:ComSpec -ArgumentList "/c", $ClaudeBin, "auth", "login" -WindowStyle Normal -PassThru
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
# A1: run login in its own normal (non-minimized) window - see the header
# comment above. -PassThru + an explicit WaitForExit() (not -Wait) so we only
# ever wait on the login process ITSELF, never a descendant browser process
# `claude auth login` may open for its OAuth flow.
$loginProcess = Start-ClaudeLogin
$loginProcess.WaitForExit()

if ((Get-ClaudeLoggedIn) -ne $true) {
    Write-Host ""
    Write-Host "Still signed out. To finish, run this in a terminal and complete sign-in:" -ForegroundColor Yellow
    Write-Host "    $ClaudeBin auth login" -ForegroundColor White
    Write-Host "LeetCoach will still open; runs work as soon as you're signed in." -ForegroundColor Yellow
}
exit 0
