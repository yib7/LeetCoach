# LeetCoach

A personal, local tool for practicing LeetCode-style problems. Paste a problem, choose a language and
a mode, and LeetCoach drives the `claude` CLI to generate study material — then saves it to `output/`
so it grows into your own study library.

> Status: under active development (autopilot build). See `.autopilot/PLAN.md` for the live plan.

## Modes
- **Learning** — teaches the full tech stack a problem needs (data structures, algorithms, stdlib
  tools), skipping topics you've already covered.
- **Guided Learning** (simple / normal / complex) — one document: restate → teach → reason → answer.
- **Answer** (simple / normal / complex) — code + step-by-step reasoning + explicit Big-O, optionally
  verified against the problem's sample I/O.

## Requirements
- Python 3.12+
- The `claude` CLI on your PATH (uses your Claude Code subscription — no API key needed).

Full setup and usage instructions land here as the build completes.
