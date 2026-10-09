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
#
# 3A S10: both waits on the CLI are bounded. `claude auth status` gets
# LEETCOACH_AUTH_STATUS_TIMEOUT_MS (default 15 s; its process tree is killed
# after that and startup simply continues), the login window
# LEETCOACH_AUTH_LOGIN_TIMEOUT_MS (default 5 min; the window is left open so
# the user can still finish signing in, but the app starts regardless).

$ErrorActionPreference = "SilentlyContinue"

# Honour LEETCOACH_CLAUDE_BIN so a non-PATH `claude` install is found the same
# way the Python app resolves it (config.claude_bin()).
$ClaudeBin = $env:LEETCOACH_CLAUDE_BIN
if ([string]::IsNullOrWhiteSpace($ClaudeBin)) { $ClaudeBin = "claude" }

function Get-TimeoutMs {
    param([string]$Value, [int]$Default)
    if ($Value -match '^\s*\d{1,9}\s*$') { return [int]$Value }
    return $Default
}

$StatusTimeoutMs = Get-TimeoutMs $env:LEETCOACH_AUTH_STATUS_TIMEOUT_MS 15000
$LoginTimeoutMs = Get-TimeoutMs $env:LEETCOACH_AUTH_LOGIN_TIMEOUT_MS 300000

function Stop-ProcessTree {
    param([int]$ProcessId)
    # /T: the whole tree - a .cmd shim's cmd.exe AND the node process it runs.
    $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
    & $taskkill /T /F /PID $ProcessId *> $null
}

function Get-ClaudeLoggedIn {
    # Returns $true / $false from `claude auth status` JSON, $null if the
    # state can't be read (binary missing, no output, unparseable), or the
    # string "timeout" if the CLI did not answer within $StatusTimeoutMs.
    #
    # Resolution goes through PowerShell's OWN command lookup (Get-Command),
    # like the `&` call operator this used before, so an ExternalScript
    # (.ps1) shim and a native Application (.exe/.cmd) both work - unlike
    # Start-Process's ShellExecute path below. 3A S10: it then runs as a
    # Process with a bounded WaitForExit instead of a plain `&` call, which
    # had no timeout and could hang app startup forever.
    $cmd = Get-Command $ClaudeBin -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { return $null }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    if ($cmd.CommandType -eq "ExternalScript") {
        $psi.FileName = Join-Path $PSHOME "powershell.exe"
        $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $cmd.Source + '" auth status'
    } elseif ($cmd.CommandType -eq "Application") {
        $psi.FileName = $cmd.Source
        $psi.Arguments = "auth status"
    } else {
        return $null
    }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    } catch {
        return $null
    }
    if (-not $proc) { return $null }
    $proc.StandardInput.Close()
    # Drain both pipes asynchronously so a chatty CLI can never fill one up
    # and deadlock against the wait below.
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $null = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($StatusTimeoutMs)) {
        Stop-ProcessTree $proc.Id
        return "timeout"
    }
    $text = ""
    if ($outTask.Wait(5000)) { $text = $outTask.Result }
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    try { return [bool]((ConvertFrom-Json $text).loggedIn) }
    catch { return $null }
}

function Test-SignedIn {
    param($State)
    return ($State -is [bool]) -and $State
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

$state = Get-ClaudeLoggedIn
if (Test-SignedIn $state) {
    exit 0  # already signed in - nothing to do
}
if ($state -is [string]) {
    # 3A S10: a status check that never answered is not "signed out" - don't
    # open a login window over it; let the app start (its banner guides).
    Write-Host ""
    Write-Host "Could not check the 'claude' sign-in in time; LeetCoach will open anyway." -ForegroundColor Yellow
    Write-Host "If runs fail with a sign-in error, run this in a terminal:" -ForegroundColor Yellow
    Write-Host "    $ClaudeBin auth login" -ForegroundColor White
    exit 0
}

Write-Host ""
Write-Host "You're signed out of the 'claude' CLI - signing you in now..." -ForegroundColor Cyan
# A1: run login in its own normal (non-minimized) window - see the header
# comment above. -PassThru + an explicit WaitForExit (not -Wait) so we only
# ever wait on the login process ITSELF, never a descendant browser process
# `claude auth login` may open for its OAuth flow. 3A S10: the wait is
# bounded; a login still open after it is left running for the user.
$loginProcess = Start-ClaudeLogin
$loginFinished = $true
if ($loginProcess) {
    $loginFinished = $loginProcess.WaitForExit($LoginTimeoutMs)
}

if (-not $loginFinished) {
    Write-Host ""
    Write-Host "Sign-in is still open in its own window - finish it there whenever you're ready." -ForegroundColor Yellow
    Write-Host "Or run this in a terminal and complete sign-in:" -ForegroundColor Yellow
    Write-Host "    $ClaudeBin auth login" -ForegroundColor White
    Write-Host "LeetCoach will still open; runs work as soon as you're signed in." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-SignedIn (Get-ClaudeLoggedIn))) {
    Write-Host ""
    Write-Host "Still signed out. To finish, run this in a terminal and complete sign-in:" -ForegroundColor Yellow
    Write-Host "    $ClaudeBin auth login" -ForegroundColor White
    Write-Host "LeetCoach will still open; runs work as soon as you're signed in." -ForegroundColor Yellow
}
exit 0
