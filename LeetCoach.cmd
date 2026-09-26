@echo off
rem #9: EnableDelayedExpansion so the errorlevel check INSIDE the block below
rem can use !errorlevel! -- %errorlevel% would be substituted just ONCE, when
rem this whole "if not exist (...)" block is first PARSED (before setup.ps1
rem even runs), silently freezing it at a stale value from before the block
rem started, rather than the real exit code afterward.
setlocal EnableDelayedExpansion
cd /d "%~dp0"
if not exist ".venv\.setup-ok" (
  echo First run: setting up LeetCoach...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  rem #9: "if errorlevel 1" only catches errorlevel >= 1 -- a crash that
  rem exits with a NEGATIVE NTSTATUS code (e.g. 0xC0000005 shows up as
  rem -1073741819) is NOT ">= 1" so the old check silently let a broken setup
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
rem so a run never fails on an expired session. Never blocks startup.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure-claude-auth.ps1"
".venv\Scripts\python.exe" app.py
rem #9: same fix as above -- a negative NTSTATUS exit code from app.py must
rem still be treated as an error, not silently pass "if errorlevel 1". Plain
rem %errorlevel% (not delayed) is fine HERE: this "if" is a top-level
rem statement read immediately after the python.exe line, not nested inside
rem an enclosing (...) block that would freeze it at parse time.
if not "%errorlevel%"=="0" (
  echo.
  echo LeetCoach exited with an error.
  pause
)
