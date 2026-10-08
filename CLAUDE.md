# LeetCoach

A personal LeetCode practice tool. Paste a problem, pick a language (C++/Java/Python) and a mode
(Learning / Guided Learning / Answer / Code Review), and it drives the **`claude` CLI** (`claude -p`, no API key —
uses the existing Claude Code subscription) to produce study material, saving it to `output/` so it
accumulates into a personal study library. Minimal local **Flask** web app (localhost).

## Environment (this machine)
- Windows 11, PowerShell primary shell (Bash tool also available).
- Python 3.12+ via the `py` launcher. Use a project `.venv`.
- Depends on the `claude` CLI being on PATH (verified at app startup).

## Commands
- Tests: `.venv/Scripts/python.exe -m pytest -q`  (all tests mock the `claude` subprocess — no real
  Claude calls; also runs the node tests when `node` is on PATH)
- JS unit tests: `node tests/js/run.js`  (zero-dependency tests for `static/lib/core.js`)
- Lint: `.venv/Scripts/python.exe -m ruff check .`
- Run: `python app.py` then open the printed `http://127.0.0.1:<port>` (5000, or the next free port)
- Click-through without real Claude: `.venv/Scripts/python.exe scripts/dev/run_fake.py` (fake CLI +
  re-seeded scratch library on :5057; `--keep` keeps it; launch config `leetcoach-fake`)

## Architecture
Tour: `docs/ARCHITECTURE.md` (routes, modules, `output/` layout). Build plan: `.autopilot/PLAN.md`. Keystone is `claude_cli.py` (subprocess wrapper around
`claude -p`, prompt piped via stdin, stream-json stdout parsed into text deltas; injectable runner so
tests mock it). The Answer-mode sandbox (`sandbox.py`) adapts STATlee's throwaway-dir/resource-capped
runner. The SSE streaming endpoint mirrors the Xeno RAG pattern (injectable `run_fn` in `create_app`).

**Autopilot runs:** the autonomy contract is `.autopilot/AUTONOMY.md` (short) — quote its two
hard-stops in every subagent brief and reference the file for the rest. The live plan + resume point
is `.autopilot/PLAN.md` (first unchecked box). Shipped history: `.autopilot/MILESTONES.md`.
