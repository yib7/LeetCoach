# LeetCoach

A personal LeetCode practice tool. Paste a problem, pick a language (C++/Java/Python) and a mode
(Learning / Guided Learning / Answer), and it drives the **`claude` CLI** (`claude -p`, no API key —
uses the existing Claude Code subscription) to produce study material, saving it to `output/` so it
accumulates into a personal study library. Minimal local **Flask** web app (localhost).

## Environment (this machine)
- Windows 11, PowerShell primary shell (Bash tool also available).
- Python 3.12+ via the `py` launcher. Use a project `.venv`.
- Depends on the `claude` CLI being on PATH (verified at app startup).

## Commands
- Tests: `python -m pytest -q`  (all tests mock the `claude` subprocess — no real Claude calls)
- Run: `python app.py` then open the printed `http://localhost:<port>`
- Lint (optional): `ruff check .`

## Architecture
See the build plan at `.autopilot/PLAN.md`. Keystone is `claude_cli.py` (subprocess wrapper around
`claude -p`, prompt piped via stdin, stream-json stdout parsed into text deltas; injectable runner so
tests mock it). The Answer-mode sandbox (`sandbox.py`) adapts STATlee's throwaway-dir/resource-capped
runner. The SSE streaming endpoint mirrors the Xeno RAG pattern (injectable `stream_fn`).

**Autopilot runs:** the autonomy contract is `.autopilot/AUTONOMY.md` (short) — quote its two
hard-stops in every subagent brief and reference the file for the rest. The live plan + resume point
is `.autopilot/PLAN.md` (first unchecked box). Shipped history: `.autopilot/MILESTONES.md`.
