@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\.setup-ok" (
  echo First run: setting up LeetCoach...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  if errorlevel 1 (
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
if errorlevel 1 (
  echo.
  echo LeetCoach exited with an error.
  pause
)
