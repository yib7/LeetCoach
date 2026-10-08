# Decisions — assumptions & reversible calls made on autopilot

Claude logs here during the run so it never stalls waiting on you. Reversible calls it already
made go under **Resolved** (with how to undo). The rare non-blocking question it wants you to
weigh in on goes under **Open** — you answer all of those in a single pass when you come back,
while it keeps running the sensible default meanwhile. Per-cycle: reset to this template when
the next cycle starts — nothing here carries forward; history is in git.

Formats — Open: `[date] <phase> — <question> — <default running meanwhile>` ·
Resolved: `[date] <phase> — <decision> — <why> — **how to undo:** <...>`

## Open (need your answer)
- (none)

## Resolved
- [2026-09-25] setup — Plan NOT supplied; ran Steps 1–2 (audit → brainstorm → plan). — user asked for a deep audit + improvements.
- [2026-09-25] setup — Merged feature/daily-use-qol → main locally (--no-ff, b29d0ae, NOT pushed) per user's answer; 315 tests + ruff green before merge. — user chose "merge first". — **how to undo:** `git checkout main && git reset --hard b54d938` (main before merge).
- [2026-09-25] setup — Cycle 11 branch `autopilot/cycle11-study-loop` off merged main. — isolation. — **how to undo:** delete branch.
- [2026-09-25] setup — `.autopilot/` stays gitignored (standing project override), so git can't archive cycle 10: copied old files to `../LeetCoach-autopilot-archive/cycle10-2026-09-08/` (outside repo) before resetting all five from templates; dropped `.autopilot/audit/`. — **how to undo:** copy the archive back.
- [2026-09-25] setup — Subagents are the default: a fresh agent per phase.
- [2026-09-25] scope — Frozen by the user via one question batch: all A+B+C findings; features D1–D7, D9–D11, D16, D6; backlog carry-overs excluded. C11 is limited to narrow-screen usability because the user didn't pick a light theme (D15). — **how to undo:** amend PLAN scope.
- [2026-09-25] design — Claude isolation uses `--safe-mode` (keeps OAuth), NOT `--bare`, which per `claude --help` (v2.1.207) never reads OAuth and would break subscription auth. Flags are gated on a help probe. Study runs keep session persistence in a neutral cwd so D6 `--resume` works. — **how to undo:** revert the SP2 argv helper.
- [2026-09-25] process — Skipped a separate brainstorming spec doc. The 4-agent audit + one user question batch served as the brainstorm, and design decisions are frozen inline in PLAN.md. — keeps it to one plan file.
- [2026-09-25] process — `.autopilot/` is gitignored, so "commit the tick" means committing the phase's code only; plan ticks live on disk.
- [2026-09-26] SP2 — One early B1 test accidentally ran the real read-only `claude auth status` once (no prompt, no cost). A guard in tests/conftest.py now blocks real claude calls in all tests. — **how to undo:** n/a.
- [2026-09-29] handoff — The user asked to continue in the cloud. Committed the SP3 B6 WIP (6f62aaa). At the user's choice, force-added .autopilot/*.md + CLAUDE.md to the branch (public repo) so the cloud session has the plan and contract. — **how to undo:** drop that commit (`git rebase --onto <commit>^ <commit>`) before merging to main, or `git rm --cached` them later.
- [2026-10-08] scope (user instruction) — Asked to "make sure LeetCoach points to the most recent models". The CLI (2.1.293) maps the aliases fable/opus/sonnet/haiku to the latest models, and the user's .env already uses `opus`. Changes: DEFAULT_MODEL `claude-opus-4-8` → `opus`; Fable added to the picker; `LATEST_MODEL_IDS` (fable 5.1 / opus 5.5 / sonnet 5.5 / haiku 5.5) used for display only. — **how to undo:** revert the feat(models) commit.
