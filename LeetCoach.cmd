@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up LeetCoach...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
)
rem Make sure the claude CLI is signed in (prompts an interactive login if not)
rem so a run never fails on an expired session. Never blocks startup.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure-claude-auth.ps1"
".venv\Scripts\python.exe" app.py
