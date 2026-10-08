# Milestones — LeetCoach

This cycle's ship record. Written at the end of the cycle (right after
`finishing-a-development-branch`), committed, and then **reset to this template when the next cycle
starts** — like every other file in `.autopilot/`. Earlier cycles live in git history
(`git log -- .autopilot/MILESTONES.md`), not here. Anything a future cycle must know belongs in
tracked `docs/` or `CLAUDE.md`, not in this file.

## Current state

LeetCoach 1.5.0 is a local Flask app that drives the `claude` CLI (subscription OAuth, no API key) to turn a
pasted LeetCode problem into Learning / Guided / Answer / Code Review study docs saved under `output/`, with
Python answers verified in a resource-capped, audit-hooked sandbox. On top of the library it now has a study
loop: problem records + an append-only run log (Stats from the log), hint ladders, re-attempt with "Test my
code", a Leitner review queue, notes, flashcards with Anki export, and follow-up chat that resumes the study
session. Windows-first; 1356 pytest + 77 node tests, all mocking `claude`.

## Cycle 11 — deep-audit remediation + the study loop — 2026-10-08 — branch `autopilot/cycle11-study-loop` → pending merge

- SP1 Launch & verification correctness — ps1 parse fix (A1), last-solution-block extraction (A2), tolerant
  output compare (A3), clean sample parsing (A4), launcher/setup hardening.
- SP2 Claude call layer & classifier — isolated `claude -p` (`--safe-mode`, no tools, strict MCP, neutral
  cwd, A7), watchdog that can't hang (A6), auth probe/error classification (B1-B3), fixed pattern list (B21).
- SP3 Sandbox hardening — job object before the go byte + fail closed (A5: 700 MB x20 contained), audit-hook
  bootstrap (no sockets/sqlite/subprocess/ctypes, writes only in the run dir), temp-dir cleanup (B6).
- SP4 Web, storage & security — atomic writes with retry (B7), safe `.env` upsert (B12), CSP/XFO + same-origin
  check (C1/C2), SSE heartbeat + cancel (C3/B14), `/healthz` single instance (D16), resolve_run_model.
- SP5 Front-end reliability, a11y & stream UX — run-id guard, model chip/phases/Stopped state (D11),
  persisted workspace + summary actions (D7), delete run, narrow-screen sidebar, `static/lib/core.js` + node tests.
- SP6 Study foundation — problem store + `runs.jsonl` (D1), Stats from the log (A8), doc contract with
  click-to-reveal insight/hints/solution (D2), real difficulty and Diff column.
- SP7 Practice loop — re-attempt + `/attempt/test` via the sandbox (D3), Leitner queue + Due today (D4), Code
  Review mode (D5), notes (D9), flashcards + Anki TSV (D10); fix round on shared notes state and save queue.
- SP8 Follow-up chat + docs — `POST /followup` with `--resume` and doc fallback (D6), README/ARCHITECTURE/
  SECURITY/CHANGELOG [1.5.0] (C13), final whole-branch review fixes (doc date kept, fence-safe verdicts,
  no double call on timeout, favicon).
- Models: aliases fable/opus/sonnet/haiku resolve to claude-fable-5-1 / claude-opus-5-5 / claude-sonnet-5-5 /
  claude-haiku-5-5 (default opus).
- Deferred / cut: D8, D12-D15, ruff 0.16, CI clean-runner, package restructure (all in BACKLOG "Next cycle").
  Known limits: `asyncio` solutions fail in the sandbox on Windows; only Python attempts are testable;
  commit a0e41d1 alone fails one version test (fixed by the next commit, 56e09ac).
- Hand-off notes for the merge: `.autopilot/*.md` and `CLAUDE.md` are tracked on this branch (fd19d94 first
  force-added them for the cloud move; every later tick commit updates them) although `.autopilot/` is
  gitignored by project policy and the repo is public. To keep them off main: `git rm -r --cached .autopilot`
  (and `CLAUDE.md` if wanted) in one commit before merging.
- cycle11 scratch swept — insights distilled (SECURITY.md, docs/ARCHITECTURE.md, CLAUDE.md, CHANGELOG); untracked SDD scratch deleted permanently.
