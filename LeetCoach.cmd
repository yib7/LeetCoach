@echo off
rem Change to this script's folder BEFORE delayed expansion is turned on, and
rem from then on name every file relative to it. With EnableDelayedExpansion
rem active, cmd.exe strips "!" out of an expanded %~dp0, so a repo under e.g.
rem C:\Users\me\a!b\ would resolve to "C:\Users\me\ab\" ("The system cannot
rem find the path specified"). The plain setlocal first keeps the cd local to
rem this script.
setlocal
cd /d "%~dp0"
rem EnableDelayedExpansion so the errorlevel check INSIDE the block below can
rem use !errorlevel!. %errorlevel% would be substituted just ONCE, when this
rem whole "if not exist (...)" block is first PARSED (before scripts\setup.ps1
rem even runs), freezing it at a stale value instead of the real exit code.
setlocal EnableDelayedExpansion
if not exist ".venv\.setup-ok" (
  echo First run: setting up LeetCoach...
  powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\setup.ps1"
  rem "if errorlevel 1" only catches errorlevel >= 1. A crash that exits
  rem with a NEGATIVE NTSTATUS code (e.g. 0xC0000005 shows up as
  rem -1073741819) is NOT ">= 1", so that check would let a broken setup
  rem fall through. Compare the literal string instead: anything other than
  rem exactly "0" is a failure. !errorlevel! (delayed expansion), not
  rem %errorlevel%, since this check is INSIDE the parenthesized block.
  if not "!errorlevel!"=="0" (
    echo.
    echo Setup failed - see the messages above for details.
    pause
    exit /b 1
  )
)
rem Make sure the claude CLI is signed in (prompts an interactive login if not)
rem so a run never fails on an expired session. Never blocks startup: the
rem script bounds both of its waits on the claude CLI.
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\ensure-claude-auth.ps1"
".venv\Scripts\python.exe" app.py
rem Same string compare as above, so a negative NTSTATUS exit code from
rem app.py counts as an error instead of passing "if errorlevel 1". Plain
rem %errorlevel% (not delayed) is fine HERE: this "if" is a top-level
rem statement read immediately after the python.exe line, not nested inside
rem an enclosing (...) block that would freeze it at parse time.
if not "%errorlevel%"=="0" (
  echo.
  echo LeetCoach exited with an error.
  pause
)
