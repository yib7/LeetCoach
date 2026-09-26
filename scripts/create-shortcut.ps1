# Places a LeetCoach shortcut on the Desktop (Cycle 10 SP-A).  Run:  .\scripts\create-shortcut.ps1
# Creates %USERPROFILE%\Desktop\LeetCoach.lnk targeting the repo-root LeetCoach.cmd,
# so the app launches from a double-click. Repeatable — re-running overwrites the .lnk.
$ErrorActionPreference = "Stop"

# Repo root is the parent of this scripts/ directory.
$repoRoot = Split-Path -Parent $PSScriptRoot
$target = Join-Path $repoRoot "LeetCoach.cmd"
$icon = Join-Path $repoRoot "docs\media\leetcoach.ico"
# Resolve the real Desktop via the known-folder API so a OneDrive-redirected
# Desktop (C:\Users\<you>\OneDrive\Desktop) is used instead of a non-existent
# C:\Users\<you>\Desktop.
$desktop = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrEmpty($desktop)) { $desktop = Join-Path $env:USERPROFILE "Desktop" }
$lnkPath = Join-Path $desktop "LeetCoach.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnkPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $repoRoot
$shortcut.IconLocation = $icon
$shortcut.Description = "LeetCoach - LeetCode practice"
$shortcut.WindowStyle = 7   # minimized, so the console doesn't steal focus from the browser
$shortcut.Save()

Write-Host "Created shortcut: $lnkPath" -ForegroundColor Green
