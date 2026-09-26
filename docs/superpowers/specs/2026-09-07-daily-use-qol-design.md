# Design — Daily-use QOL cycle (Cycle 10)

**Date:** 2026-09-07
**Branch:** `feature/daily-use-qol` (off `main` @ b54d938, released v1.3.3)
**Target release:** v1.4.0 (minor — first user-facing feature cycle since Quick Ask)

## Goal

Make LeetCoach frictionless to open and rewarding to return to, so the user actually
practices every day instead of being demotivated by launch overhead. Three levers:

1. **Kill launch friction** — one double-click on a desktop icon starts the app and opens
   the browser. No terminal, no venv activation, no manual navigation, no port crash.
2. **Give a reason to come back** — a real Stats tab (streak, activity, totals) derived from
   the study library the user already accumulates, plus an at-a-glance streak badge.
3. **Make the UI honest** — every button works. Fix the broken "? for shortcuts" promise,
   make ⌘K search real, and remove the remaining dead placeholder controls.

Plus a systematic bug pass across the new and touched surface.

## Out of scope (YAGNI — parked in BACKLOG)

- **No backend run-history store.** Stats are derived client-side from `/library` file
  mtimes; we do NOT add a persistence layer (the BACKLOG "backend store" idea stays parked —
  the derived version is lighter and needs no new endpoint).
- **No real difficulty signal** (Easy/Med/Hard needs LeetCode metadata capture — BACKLOG).
- **No C++/Java auto-verification** (P2-10 — BACKLOG).
- **No bookmarking feature** — we remove the dead Bookmarks button, we don't build the feature.
- **No package restructure, no multi-user/accounts/network** — localhost single-user tool.

## Guiding constraints (unchanged invariants)

- Fully offline; no runtime CDN; all tests mock the `claude` subprocess.
- Existing CSP / `nosniff` headers must keep passing; new assets are same-origin (`/static/`).
- The Host-header allowlist (`127.0.0.1`, `localhost`, `[::1]`) is **hostname-only, any
  port** — so port fallback does not weaken the DNS-rebinding defense.
- Front end stays: raw Flask template + one `style.css` + one `app.js`, vendored libs only.
- Tests stay green throughout (baseline: **281 passed**). New Python surface gets tests;
  JS is browser-verified against a seeded scratch library (consistent with all prior UI cycles).

---

## Phase A — One-click launch

**Checkpoint (observable done):** `.venv\Scripts\python.exe app.py` starts, auto-opens the
browser to the actual chosen port, and when port 5000 is occupied it selects a free port
instead of crashing; `LeetCoach.cmd` launches the app from a double-click; `create-shortcut.ps1`
produces a working `Desktop\LeetCoach.lnk`. Full suite + new port-helper tests green.

### A1 — Server-side port fallback + auto-open browser (`app.py`, `__main__`-guarded)
- Add a module-level pure helper `_choose_port(preferred, host, *, span=20)`: probe-bind a
  socket to `(host, preferred)`; if free, return it; else return the first bindable port in
  `preferred+1 … preferred+span`; else bind to port `0` and return the OS-assigned ephemeral
  port. (Probe socket is closed before returning; a small TOCTOU window is acceptable for a
  single-user tool.)
- In the `if __name__ == "__main__"` block only (so imports/tests are untouched): choose the
  port, print the real URL, and schedule `webbrowser.open(url)` on a ~1s `threading.Timer` so
  it fires after the server is accepting connections. Suppress with env
  `LEETCOACH_NO_BROWSER` ∈ {1,true,yes} (headless/dev). No change to `app.run` semantics
  otherwise (debug=False, threaded=True).
- **Tests (Python, CI-safe, no browser/claude):** `_choose_port` returns the preferred port
  when free; when the preferred port is bound by the test, returns a *different* port that is
  itself bindable. Auto-open logic stays `__main__`-only and is not unit-tested (never
  imported by pytest).

### A2 — `LeetCoach.cmd` launcher (repo root, next to `setup.ps1`)
- Double-click target. `cd /d "%~dp0"` into the project; if `.venv\Scripts\python.exe` is
  missing, run `setup.ps1` first (first-run bootstrap); then `".venv\Scripts\python.exe" app.py`.
- Runs python in the foreground of the console, so **closing the window stops the server**
  (intuitive). Lives at root next to `setup.ps1` for symmetry (both user-facing entry points).

### A3 — `scripts/create-shortcut.ps1`
- Uses `WScript.Shell` COM to create `%USERPROFILE%\Desktop\LeetCoach.lnk` → target
  `LeetCoach.cmd`, working dir = repo root, icon = generated `.ico` (A4), description set,
  window style minimized so the console doesn't steal focus from the browser.
- Committed and repeatable. The orchestrator runs it once during Phase D to actually place
  the shortcut on the user's Desktop (explicitly requested; local + reversible → proceed and
  log to DECISIONS).

### A4 — Icon + favicon
- Generate `docs/media/leetcoach.ico` (multi-size 16/32/48/256) from the `[lc]` brand mark
  via Pillow at build time; commit the `.ico` (Pillow is not a runtime dep). If Pillow is
  unavailable, install it into the venv for the one-time generation only.
- Add a browser-tab **favicon**: `static/favicon.svg` (offline, no binary), referenced from
  the template `<link rel="icon">`. Must comply with the existing CSP (same-origin `/static/`
  is covered by `img-src 'self'`; verify during implementation).

---

## Phase B — Motivation: real Stats tab + streak

All derived client-side from `/library` mtimes — **no backend change, still fully offline.**

**Checkpoint:** the Stats view renders real numbers from a seeded library (current & longest
streak, solved today/this week/total, activity heatmap, per-mode/-language/-topic breakdowns);
the Console header shows a 🔥 streak badge; the empty-library state is graceful; no console
errors. Browser-verified against a seeded scratch `output/`.

### B1 — Stats computation (`app.js`, pure functions)
- Extract pure helpers operating on the already-derived `currentRuns` (from `deriveRuns()`
  over `/library`; confirm each run's shape during implementation — expected: an mtime-based
  timestamp, mode, language, topic/type, title):
  - `dayKey(date)` → local `YYYY-MM-DD`.
  - `computeStats(runs, now)` → `{ total, distinctProblems, today, thisWeek, currentStreak,
    longestStreak, byMode, byLanguage, byTopic(sorted desc), heatmap:[{date,count}] }`.
- **Streak:** build a Set of active local day-keys. `currentStreak` counts consecutive days
  ending today; if today has no activity but yesterday does, start from yesterday (standard
  "today-or-yesterday grace" so a fresh morning doesn't read 0). `longestStreak` = longest
  consecutive run over the sorted unique days.
- **Heatmap:** last ~17 weeks (≈119 days), GitHub-style grid (columns = weeks, rows = weekday),
  5 intensity buckets by runs/day.

### B2 — Stats view (`index.html` + `app.js` + `style.css`)
- New content section `#view-stats` beside `#view-console` / `#view-library`, matching the
  existing design system (panels, chips, tokens).
- Routing: **both** the topbar "Stats" nav item and the sidebar "Stats" item lose `inert`,
  gain click handlers, and wire into the existing view-switch + active-state logic. (Phase B
  owns all Stats-related `index.html` edits.)
- Layout: a row of stat tiles (🔥 current streak, longest streak, solved today, total solved
  with a distinct-problems subtitle) → the activity heatmap panel (with Less→More legend and
  month labels) → breakdown panels (by mode with bars, by language, top topics).
- **Empty state:** no runs → friendly card ("No runs yet — solve your first problem to start
  your streak 🔥") with a button back to Console.
- Recompute on load and after each run (hook the existing `loadLibrary()` completion).

### B3 — Console streak badge (`index.html` + `app.js` + `style.css`)
- A subtle badge in the Console context header (`.ctx` right side): `🔥 N-day streak`
  (optionally `· M today`), refreshed whenever stats recompute. Streak 0 → a quiet "Start your
  streak today" nudge rather than a bare 0. Visible every time the app opens.

---

## Phase C — Real ⌘K search + shortcuts help + de-cruft

Front-end only. **Builds on Phase B's committed `index.html`/`app.js`/`style.css`** (sequential
— coherence flows through the filesystem, per the Cycle-3 single-file-subagent precedent).

**Checkpoint:** `?` opens a shortcuts modal listing accurate shortcuts and Esc/backdrop closes
it (and does NOT fire while typing in the problem box); ⌘/Ctrl+K opens a search palette that
filters real saved runs/library and opens a selected file; Bookmarks + the inert "Clear" link
are gone; no console errors.

### C1 — Keyboard-shortcuts help modal (fixes the broken `?` promise)
- Add a hidden modal `#shortcuts-modal` listing the **real** shortcuts: ⌘/Ctrl+Enter Run,
  ⌘/Ctrl+K Search, `?` Shortcuts, Esc Close, Enter (Quick Ask) Ask.
- Triggers: global keydown `?` (Shift+/) opens it, **guarded** so it never fires while focus is
  in an `<input>`/`<textarea>`/contenteditable (the problem box); the previously-inert shortcuts
  icon button becomes the click trigger (remove `inert`). Esc and backdrop click close.
- A11y: `role="dialog"`, `aria-modal`, focus the close control on open, restore focus on close,
  toggle via `hidden`/`.open`. Fixes the `index.html` footer "? for shortcuts" promise (which
  stays, now accurate).

### C2 — ⌘K Search palette (make the top-bar search real)
- Remove `inert` + the "not available yet" title from the top-bar search box.
- A lightweight overlay **palette**: ⌘/Ctrl+K opens it focused; typing filters `currentRuns` +
  library files by title / topic / mode / language (case-insensitive substring); results list
  is keyboard-navigable (↑/↓, Enter opens, Esc closes); selecting opens the file in the Library
  viewer (reuse the existing open-file + view-switch path).
- User-derived strings rendered via `textContent` only (no innerHTML injection). Palette chosen
  over in-place table filtering because it matches the top bar's ⌘K affordance and doesn't
  disturb the Console layout.

### C3 — De-cruft
- Remove the sidebar **Bookmarks** item and the inert **"Recent runs → Clear"** link.
- The top-bar search box is no longer `inert` (C2). Keep the now-accurate "? for shortcuts"
  footer hint.

---

## Phase D — Bug hunt & final verification

**Checkpoint:** full suite (281 + new port tests) green; ruff clean; browser walkthrough of
launcher (incl. port fallback + auto-open), Stats (numbers/heatmap/streak badge/empty state),
search palette, and shortcuts modal all pass; the Desktop shortcut is placed and verified;
README + CHANGELOG updated; branch handed to the human for the merge decision.

- Hold the green baseline throughout; add the new Python tests (port helper).
- **Systematic bug pass** over new/touched surface: empty library, single run, midnight/timezone
  boundary, streak grace edge, search XSS via `textContent`, the `?`-while-typing guard, modal
  focus/Esc/backdrop, double browser-open, port TOCTOU, any regression in the existing recents/
  library/derive flow. Fix with TDD where Python; browser-verify where JS.
- Run `scripts/create-shortcut.ps1` to actually place `Desktop\LeetCoach.lnk`; confirm it exists
  and its target/working-dir resolve; log to DECISIONS.
- Docs: README "daily use / desktop shortcut" note + `## Configuration` gains `LEETCOACH_NO_BROWSER`;
  CHANGELOG `[1.4.0]` (unreleased) covering launcher, Stats/streak, search, shortcuts help, de-cruft.
- Finish: `superpowers:finishing-a-development-branch` → present merge/PR options. **Merging to
  `main` stays a human decision.**

## Testing strategy summary

- **Python (pytest, offline):** `_choose_port` behavior (preferred-free, fallback-when-busy).
  Everything else in Phases B/C is client-side and adds no endpoint, so no new server tests there.
- **JS (browser pane, seeded scratch `output/` at controlled mtimes, zero `claude` calls):**
  Stats numbers/streak/heatmap/empty-state, streak badge, ⌘K search opening files, shortcuts
  modal open/close/guard, launcher port-fallback + auto-open.
- **Regression:** the 281-test suite stays green at every phase boundary; ruff clean.
