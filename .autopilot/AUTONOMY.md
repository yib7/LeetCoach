# Autopilot — Autonomy Contract

This run is on **autopilot**. The orchestrator and every subagent obey this. It's short on purpose:
quote the two hard-stops verbatim in each subagent brief and point here for the rest.

**Hard-stop — ask the human, then wait — ONLY for:**

1. **Secrets / credentials** — needing, creating, printing, committing, or rotating an API key,
   token, password, private key, or `.env` secret.
2. **Real money** — spending actual money / paid API credits, placing an order, incurring billable
   cloud cost.

Those two are the only mid-loop hard-stops. Everything else is either a proceed-and-log default or
an out-of-loop action (both below) — never a reason to pause just to ask permission.

**Everything else: pick the sensible default, proceed, and log one line to `DECISIONS.md`** — this
includes reversible-but-scary and destructive-*local* ops **whose undo is a git operation on this
worktree** (refactors, file moves/deletes, schema changes, dropping a local/test DB, rewriting
**un-pushed** git history, resetting the working tree on this branch). If the undo is *not* a git
operation on this worktree — a global tool install, shared dotfile/config edits, dropping a DB other
projects share — isolation does **not** cover it: prefer a worktree-local alternative, otherwise
treat it as out-of-loop (below).

**Isolation is the safety net — but only for state this worktree owns.** Work happens on a dedicated
worktree/branch — never `main`, never a live deployment. **Out of loop — the human does these, not
you:** merge to `main`, deploy, run against production, `push --force` to a shared branch — and
anything that *leaves the machine*: pushing to a shared remote, opening a PR or issue, `npm publish`
/ any publish, posting or messaging through an external or MCP tool. Deleting a branch can't undo
those, so they are never a mid-run default — stop and note them for the human at the final gate.

**Logging:** reversible decision → `DECISIONS.md` Resolved (`[date] <phase> — <decision> — <why> —
how to undo: <...>`); a rare non-blocking question → `DECISIONS.md` Open (keep running the default
meanwhile); a new unrelated idea → `BACKLOG.md` Inbox (next cycle, not this run).
