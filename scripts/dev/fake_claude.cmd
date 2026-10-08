@echo off
rem Fake `claude` CLI shim for LeetCoach dev/browser verification.
rem NEVER calls real Claude: it runs scripts\dev\fake_claude.py, which prints
rem canned stream-json. Point LEETCOACH_CLAUDE_BIN here (run_fake.py does).
rem Uses FAKE_CLAUDE_PYTHON when set, else the project .venv interpreter.
setlocal
if defined FAKE_CLAUDE_PYTHON (
  set "FAKE_PY=%FAKE_CLAUDE_PYTHON%"
) else (
  set "FAKE_PY=%~dp0..\..\.venv\Scripts\python.exe"
)
"%FAKE_PY%" "%~dp0fake_claude.py" %*
exit /b %ERRORLEVEL%
