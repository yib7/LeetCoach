@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up LeetCoach...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
)
".venv\Scripts\python.exe" app.py
