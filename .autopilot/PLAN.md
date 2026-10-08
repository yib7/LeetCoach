# PLAN — LeetCoach Cycle 11: deep-audit remediation + the study loop (target v1.5.0)

> On autopilot. Resume point = the first unchecked box below. Isolated on branch
> `autopilot/cycle11-study-loop` (never `main`; cut from main @ b29d0ae after the local v1.4.0 merge).
> Autonomy contract: `.autopilot/AUTONOMY.md` (hard-stops: 1. secrets/credentials, 2. real money).
> New ideas → `.autopilot/BACKLOG.md`. Reversible decisions → `.autopilot/DECISIONS.md`.
> Ship record → `.autopilot/MILESTONES.md`. `.autopilot/` is gitignored (project policy) — cycle 10
> archive lives at `../LeetCoach-autopilot-archive/cycle10-2026-09-08/`.
> **Finding IDs (A1…, B1…, C1…, D1…) refer to the audit appendix at the bottom of this file.**

## Scope (frozen 2026-09-25 by the user)

Fix **every** audit finding (A high, B medium, C low/polish — C11 limited to narrow-screen usability; a
light theme is OUT) and build the study loop: D1 problem record + run log, D2 study-doc contract + hint
ladder, D3 re-attempt + "Test my code", D4 spaced-repetition review queue, D5 Code Review mode, D6
follow-up chat, D7 workspace persistence + summary actions, D9 notes, D10 flashcards + Anki export, D11
stream UX/phase events, D16 single-instance launcher.
**OUT (parked in BACKLOG):** D8 progress-by-pattern view, D12 timer, D13 harness verify, D14 C++/Java
verify, D15 light theme/full responsive, ruff 0.16, package restructure, CI clean-runner job.

## Global constraints (every task inherits these)

- Fully offline front end; raw Flask template + `static/style.css` + `static/app.js`; vendored libs only;
  no new runtime deps unless unavoidable (log it). Strict CSP stays; build model-derived DOM via `el()`/
  textContent, never innerHTML with model/user text.
- **All tests mock `claude`.** Never invoke the real CLI (no `claude -p`, no smoke script). Baseline **315
  passed**, `ruff check .` clean (venv ruff 0.15.x). Run with `.venv/Scripts/python.exe -m pytest -q`.
- Windows-first (PowerShell 5.1 compatibility for `.ps1`; keep `.ps1`/`.cmd` ASCII-only), Python 3.12+.
- Backward compatible with existing `output/` libraries (no destructive migration; legacy files keep
  working; new metadata lives under `output/.leetcoach/`, hidden from the library tree/search).
- Conventional-commit messages matching history; **no attribution lines**; commit per task/phase.
- Hard-stops (verbatim from AUTONOMY.md): "1. **Secrets / credentials** — needing, creating, printing,
  committing, or rotating an API key, token, password, private key, or `.env` secret. 2. **Real money** —
  spending actual money / paid API credits, placing an order, incurring billable cloud cost."

## Frozen design decisions

- **Claude invocation (A7/D6):** helper builds argv; flags gated on a cached `claude --help` probe (only pass
  a flag the installed CLI lists). Study runs: `--safe-mode` (keeps OAuth; NEVER `--bare`, it drops OAuth),
  `--tools ""`, `--strict-mcp-config`, `--system-prompt <tutor persona>`, `cwd` = neutral dir
  `LEETCOACH_CLAUDE_CWD` (default `%LOCALAPPDATA%\LeetCoach\claude-cwd`, `~/.local/share/leetcoach/claude-cwd`
  elsewhere), **session persistence ON** (needed for D6 `--resume`; lives under the neutral dir's project
  bucket, not the repo's). Classifier / Quick Ask / auth probe: same isolation + `--no-session-persistence`.
  Capture `session_id` from stream-json (`system/init` or `result`).
- **Metadata store (D1):** `output/.leetcoach/problems/<problem_id>.json` = {id, number, title, difficulty,
  pattern, statement, created, notes, review:{box, due, history[]}, runs[] (paths)}; `output/.leetcoach/
  runs.jsonl` append-only {ts, problem_id, mode, language, tier, model, verdict, files, session_id,
  duration_s}. problem_id = `<number>-<slug>` when a number is parsed from the paste, else slug; NFKD
  transliteration + short hash suffix when the ASCII slug is empty (B25). Difficulty/number/title parsed
  from the paste locally (no network). One shared atomic-write helper (tmp + `os.replace` with
  PermissionError retry/backoff, tmp cleanup) used by storage, topic index, problem store, `.env`.
- **Stats (A8):** computed from `runs.jsonl`; legacy files without log entries count per-file by their own
  mtime (no stem collapsing; trailing `__\d+` = slot, not tier).
- **Pattern list (B21/D2):** fixed ~18-pattern list shared by classifier + prompt contract; free-form
  classifier output mapped/normalized onto it; topics sanitized (≤40 chars, `[a-z0-9 _+-]`), language-keyed.
- **Doc contract (D2):** shared fragment for all modes: `# <n>. <Title>` line, `Pattern: … · Difficulty: …`,
  fixed H2 sections (Problem in brief · Constraints → target complexity · How to recognize this pattern · Key
  insight · Approach · Solution · Complexity · Edge cases · Common mistakes · Related problems · Flashcards),
  modes omit what they forbid; no questions to the reader; exactly one solution block tagged
  ```` ```<lang> solution````. Guided/Learning emit `### Hint 1..4`; client renders Hint sections (and the
  Guided Solution) as click-to-reveal. Learning must not contain an end-to-end solution.
- **Review queue (D4):** Leitner intervals [1,3,7,14,30] days; grades: solo → box+1, with-hints → same box,
  peeked/failed → box 1; a problem enters the queue (due +1 day) on its first saved run.
- **Code Review mode (D5):** new mode `review` with a second "Your code" textarea; saved to
  `output/reviews/<pattern>/<stem>__review.md`.
- **Follow-up (D6):** `POST /followup` {path, question} → SSE; `claude -p --resume <session_id>` in the same
  neutral cwd; on resume failure fall back to a fresh isolated call with the doc as context; answer appended
  to the doc under `## Follow-up — <question>`.
- **Single instance (D16):** `GET /healthz` → {"app":"leetcoach","version"}; launcher/`__main__` probes the
  preferred port first; if LeetCoach answers, open the browser there and exit.

---

## SP1 — Launch & verification correctness

**Checkpoint:** suite green (315 + new); every `.ps1` parses under `powershell.exe` 5.1 (scripted check);
verification of a multi-block Guided doc with `[0, 1]`-style output and `**Input:**` labels passes; ruff clean.

- [x] A1 — `ensure-claude-auth.ps1` ASCII-only; honour `LEETCOACH_CLAUDE_BIN`; `claude auth login` in a
  visible window (`Start-Process -Wait`) not the minimized launcher console (B9). Add `tests/test_scripts.py`
  (skipped off-Windows) that parses every `.ps1` with `[Parser]::ParseFile` under `powershell.exe` and asserts
  all `.ps1`/`.cmd` are ASCII; add the same parse step to CI (C14, windows job).
- [x] B9 — `setup.ps1`: `$PSScriptRoot`-relative, check `$LASTEXITCODE` after native calls, Python ≥3.12 check,
  write `.venv\.setup-ok` only on success; `LeetCoach.cmd` re-runs setup when the marker is missing and
  `pause`s on non-zero app exit. C8: `.gitattributes` `*.cmd text eol=crlf`, `*.ps1 text eol=crlf`; README
  shows `powershell -ExecutionPolicy Bypass -File …` invocations.
- [x] A2 — `extract_code`: prefer block tagged `<lang> solution`, else the last language-tagged block containing
  `__main__`/`class Solution`/`def`, else last tagged block; indentation-aware fences. Tests: multi-block Guided,
  indented list fences, legacy single block.
- [x] A3 — structural comparison in `sandbox`: exact-normalized first, then tolerant parse (JSON / Python
  literal via `ast.literal_eval`, `True/False/None`↔`true/false/null`, whitespace in lists, unquoted vs quoted
  strings, float tolerance 1e-5); no order-insensitivity unless expected text is identical as multiset AND the
  problem says "any order". Tests incl. the real `two_sum` `[0, 1]` vs `[0,1]` case.
- [x] A4 — `parse_samples` strips `*`/`_`/backticks around labels & values; driver prompt says read ALL stdin.
  B4 — keep per-sample note (timeout/crash reason, stdin/expected/partial stdout) through aggregation and into
  the saved `.md`. B5 — float timeouts end-to-end; `config` clamps timeouts to (0, 86400], rejects inf/nan.
- [x] B23 — non-object JSON bodies → 400 on every JSON route. B24 — no code block → don't write an empty code
  file. B8 — autouse test fixture isolating ALL `LEETCOACH_*` env to `tmp_path` + `LEETCOACH_NO_DOTENV=1`
  honoured by `app.py`. C7 — `.env.example` comments out path settings, lists all settings incl.
  `LEETCOACH_NO_BROWSER`.
- [x] Tests: new cases above green; full suite + ruff green; ps1 parse check run on this machine.
- Verified: 404 passed, ruff clean, all .ps1 parse under PS 5.1 (0 errors), real topic_index untouched; review→2 fix rounds→clean. Commits 33f768f..f9d49b2.

## SP2 — Claude call layer & classifier

**Checkpoint:** argv tests prove isolation flags + neutral cwd (flag-gated); watchdog test with a
pipe-holding grandchild finishes within timeout+slack; parser fuzz cases pass; suite + ruff green.

- [x] A7 — isolation per design (help-probe gating, `--safe-mode`, `--tools ""`, `--strict-mcp-config`,
  `--system-prompt` tutor persona moved out of the user prompt where sensible, neutral cwd, persistence rules),
  capture `session_id`; new config `LEETCOACH_CLAUDE_CWD`.
- [x] A6 — watchdog keyed on `reading_done` only; stop reading after the `result` event; `claude` tree in a
  kill-on-close job (Windows) / process group (POSIX, C5) and `proc.kill()` after taskkill; classifier join
  bounded (~60 s) and classifier process cancelled on disconnect.
- [x] B1 — auth probe: Popen + tree-kill on timeout, `encoding="utf-8", errors="replace"`, 60 s cache, not on
  every `GET /` synchronously (C15-style: cached). B2 — error headline from the real stderr; "signed out" only
  when an auth marker is present. B3 — type-guard every stream event; `is_error` result → raise. C4 — no
  spurious raise on disconnect during stdin write.
- [x] B21 — fixed pattern list + normalization; topics sanitized/capped; `raw_decode`-based JSON extraction;
  nonce delimiters for pasted problem text in all prompts. B22 — topic index language-keyed (legacy entries
  migrate as language-agnostic), Guided passes learned topics and records topics.
- [x] Tests: argv/cwd, flag gating, watchdog grandchild, parser fuzz, classifier normalization, delimiter
  forging, auth probe timeout/encoding; suite + ruff green.
- Verified: 628 passed, ruff clean; A7 bc95e6a + remaining items; review→fix round (I1 grace-dead, I2 kill race + 6 minors)→approved→minor round. Commits bc95e6a..2389d89.

## SP3 — Sandbox hardening

**Checkpoint:** a 700 MB allocation is killed by the cap in ≥20/20 runs under 2 busy parent threads (scratch
stress script, recorded in DECISIONS); grandchild-held pipe no longer loses stdout; temp dirs cleaned; suite green.

- [x] A5 — bootstrap: spawn `sys._base_executable -I` running a trusted bootstrap that waits for a "go" byte;
  parent assigns the job object, then releases; bootstrap `runpy.run_path`s the solution.
- [x] C6 — bootstrap installs `sys.addaudithook` blocking `open` outside the run dir (write) and of known secret
  paths, `socket.connect`, `subprocess`/`os.system`/`os.exec*` (defence-in-depth, documented in SECURITY.md as
  not a boundary).
- [x] B6 — raw byte reads (`os.read`/`read1`) with caps, decode at end; `rmtree` retry/backoff after kill; sweep
  stale `leetcoach_run_*` older than 1 day at startup.
- [x] Tests: bootstrap ordering, audit-hook blocks, output capture with grandchild, cleanup; suite + ruff green.
- Verified: 2026-10-08 orchestrator re-run at 3b6bb9b: 768 passed/1 skipped, ruff clean, 700 MB stress contained 20/20 with 2 busy parent threads, 0 leftover run dirs; review approved after 3 fix rounds (fail-closed caps, READY/go handshake, sqlite+all sockets blocked; asyncio unsupported in sandbox).

## SP4 — Web, storage & security

**Checkpoint:** Flask test-client tests for headers, Origin rejection, heartbeat, cancel, healthz,
single-instance, `.env` edge cases, atomic writes under a held reader, non-ASCII slugs, save fallback; suite green.

- [x] Shared atomic-write helper (`fsutil.py`) used by storage / topic index / `.env`. B7 — topic index retries,
  cleans tmp, preserves a corrupt file as `.corrupt-<ts>` instead of overwriting.
- [x] B12 — `.env` upsert: `utf-8-sig`, UTF-16 tolerant read, replace all dup keys, abort on read error, atomic;
  `/run` accepts an optional per-run `model` validated against the allowlist (UI sends it).
- [x] B25 — NFKD slugs + hash fallback; save OSError → fallback `output/_unsorted/<hash>.md` + clear UI message;
  Answer code+md pair written atomically.
- [x] B10 — library cache signature over all dir mtimes (or short TTL). B19 (server) — hide `.leetcoach/` and
  `topic_index.json` from listing/search/delete; `DELETE` supports deleting a whole run (md + code siblings).
- [x] C1 — `frame-ancestors 'none'` + `X-Frame-Options: DENY`. C2 — reject non-loopback `Origin` /
  `Sec-Fetch-Site: cross-site` on unsafe methods (and on `GET /` auth probe path). C3/B14 — SSE `: ping` every
  ~15 s; `POST /run/cancel` frees the in-flight slot and kills the process.
- [x] D16/B11 — `/healthz`; single-instance reuse in `__main__` before port fallback.
- [x] Tests for all of the above; suite + ruff green.
- Verified: 2026-10-08 orchestrator re-run at 5a20601: 909 passed/1 skipped, ruff clean, node --check OK, .env upsert repros preserve all other lines (tmp files), real .env untouched; review approved after 2 fix rounds (pinned model kept, cancel/commit race, cancellable verify, .env unclosed-quote data loss).

## SP5 — Front-end reliability, a11y & stream UX

**Checkpoint:** `node --check` OK; browser-verified (preview, seeded scratch output dir, `claude` stubbed via a
fake bin — never real) for: run-id guard, Stop→re-run, reload persistence, throttled render, verdict colours,
keyboard nav, focus trap, narrow-width sidebar access; zero console errors; suite green.

- [x] B13 run-id guard; B14 client (Stop calls cancel; 409 → retry w/ backoff + message); B17 truthful
  "stream ended unexpectedly"; B20 Quick Ask cancel + 60 s client timeout; B12 client `resp.ok` + revert +
  model chip on runs, showing the concrete model from the stream-json `system/init` event (e.g. "Opus 5.5") because the picker passes aliases.
- [x] B15/D7 — localStorage (try/catch) for draft/mode/lang/tier/QA-collapsed; `beforeunload` guard while
  streaming; summary actions: Open in Library, Re-run as Optimal, Re-run in other language, Learn this topic.
- [x] B16 — render throttle (~150 ms), highlight on final render / closed blocks only, untagged → plaintext.
  D11 — auto-follow + "Jump to latest" pill; SSE `phase` events (streaming → verifying i/n → saving) rendered.
- [x] B18 real status column + coloured FAIL/not-verified chips; B19 client "Delete run"; C9 a11y set
  (aria-busy + separate live region, button rows, working topic chips → ⌘K prefilled, aria-pressed/disabled,
  `--tx4` contrast ≥4.5:1, focus trap + aria-activedescendant, labels, IME guard).
- [x] C10 set (Ctrl+Enter switches to Console, library scroll reset + active highlight, per-view scroll, copy
  label, platform glyphs, remove fake gutter, visible failures, title captured at run start, no Stats empty-
  flash, autolink `&`); C11 narrow screens (≤900 px sidebar drawer/toggle, topbar wraps, table min widths,
  library stacks); C12 dead CSS/vars removed.
- [x] Tests: add `tests/js/` node-runnable unit tests (no deps) for pure helpers where feasible + wire into
  pytest via a skip-if-no-node test; browser verification recorded.
- Verified: 2026-10-08 orchestrator at 55d0511: 966 passed/1 skipped, ruff clean, node 32/32; browser-verified on the fake harness (never real claude): model chip pending->Opus 5.5, PASS/FAIL chip colours, summary actions, Jump-to-latest, Stop->'Stopped' (no errbox) + immediate re-run, cut stream -> error and nothing saved, reload persistence, Quick Ask Cancel/Esc + 'Quick Ask cancelled.', Delete run modal (focus trap, both files deleted), Ctrl+K aria-activedescendant, <=900px drawer + Esc + focus return, no h-scroll, Ctrl+Enter from Library, table slug ellipsis, zero console/server errors. Review approved with minors, all fixed.

## SP6 — Study foundation: problem record, run log, doc contract, hints

**Checkpoint:** a mocked run writes `problems/<id>.json` + a `runs.jsonl` line; Stats computed from the log
(legacy fallback) with the A8 4-day scenario giving streak 4; prompt tests assert the contract; hint/solution
reveal browser-verified; suite green.

- [x] D1 — problem store + run log (design above); parse number/title/difficulty from the paste; Diff column real;
  `/problems` + `/problems/<id>` JSON endpoints; problem statement saved.
- [x] A8 — Stats from `runs.jsonl` (+ per-file legacy) — server-computed `/stats` endpoint or client from log.
- [x] D2 — shared doc-contract fragment across modes, Learning no-solution rule, Guided brute→optimal + Hints,
  single `<lang> solution` block, JSON-style printing guidance; client click-to-reveal for Hint sections and
  Guided Solution (DOM-built).
- [x] Tests: store/log unit tests, stats scenarios, prompt contract tests, render helper tests; suite + ruff green.
- Verified: 2026-10-08 orchestrator at aef811b: 1085 passed/1 skipped, ruff clean, node 52/52; browser-verified on the fake harness: Diff column, #N title badges + '#20 · Valid Parentheses' viewer title, /problems runs/run_count/file_count, Stats from log+legacy, Guided reveals (insight, hint-1..4, solution) closed with sr-only headings and no heading inside summary, hint-4 holds only its first block when no heading follows (seeded doc + live FAKE_NOWALK run), Walkthrough visible, Learning has Techniques and no solution code, live Guided run logs tier null with no tier chip or Re-run-as-Optimal, paste difficulty Medium (source paste); only console error was the orchestrator's own 404 probe. Review: Important I1 + minors fixed.

## SP7 — Practice loop: re-attempt, test my code, review queue, code review, notes, flashcards

**Checkpoint:** browser-verified end-to-end with stubbed `claude`: open due problem → re-attempt → Test my code
(Python sandbox, samples + custom case) → self-grade → due date moves per Leitner; Code Review mode run saved;
notes persist; flashcards review + TSV export download; suite + ruff green.

- [ ] D3 — Re-attempt view (statement, code textarea w/ Tab support, language), `POST /attempt/test` (Python via
  sandbox; others → clear "not supported yet"), give-up → reveal latest doc.
- [ ] D4 — review scheduling + grading endpoint; Console "Due today (N)" panel; Stats shows review counts.
- [ ] D5 — Code Review mode (prompt, second textarea, storage path, library/stats aware).
- [ ] D9 — notes editor in library viewer for problem-linked docs (saved in the problem record).
- [ ] D10 — flashcards parsed from `## Flashcards`; in-app flip review; `GET /flashcards.tsv` Anki export.
- [ ] Tests for endpoints, scheduling math, parsing; suite + ruff green.

## SP8 — Follow-up chat, docs, final verification

**Checkpoint:** follow-up works with mocked resume + fallback; README/ARCHITECTURE/SECURITY/CHANGELOG [1.5.0]
accurate; full suite + ruff green from committed HEAD; browser walkthrough of every new feature with zero
console errors; MILESTONES written; branch handed to human.

- [ ] D6 — `POST /followup` SSE with `--resume <session_id>` + fallback; append to doc; UI box in viewer.
- [ ] C13 — docs drift fixed (auth claims, ARCHITECTURE routes/config, Quick Ask model tag from config, stale
  comments); README features + settings table; CHANGELOG `[1.5.0]`; SECURITY.md sandbox/isolation notes.
- [ ] Final: full suite + ruff + ps1 parse + node tests; browser walkthrough; `requesting-code-review` over the
  whole branch diff and fix findings; `finishing-a-development-branch` (merge = human gate).

## Blocked (filled in during the run)

- [2026-09-29] MOVED TO CLOUD mid-SP3 (user request). SP3 commits: ce2bc68 (A5 bootstrap handshake), efd847e (C6 audit
  hook), f71e237 (SECURITY.md), 6f62aaa (B6 WIP, committed unreviewed; 682 passed + 1 skipped on Windows). On resume: finish or
  fix B6, then review SP3 over 2389d89..HEAD; no SP3 box is ticked yet. CLOUD CAVEATS: the cloud runner is Linux, so
  Windows-only checks skip there: the job-object memory cap, the SP3 checkpoint "700 MB x20 under 2 busy threads", the .ps1
  parse checks, and the LeetCoach.cmd tests. Verify the POSIX rlimit path in the cloud and record the Windows stress run as
  "pending local verification" in DECISIONS. Final sign-off must re-run the full suite on Windows. Local-only paths in this
  plan (the ../LeetCoach-autopilot-archive and .superpowers/sdd briefs) don't exist in the cloud: read the SPn sections of this
  file directly as the phase brief. The venv is `.venv` on Windows; in the cloud create one and use `python -m pytest -q`.
  The /autopilot skill and superpowers plugin may be absent in the cloud; AUTONOMY.md + this plan are the contract.
  These .autopilot/ files and CLAUDE.md were force-committed so the cloud session has them (the user chose this; the repo is
  public). Consider dropping that commit before the human merges to main.

- [2026-09-26] (resolved) RESUMED by the user; SP2 re-dispatched for the remaining items on top of bc95e6a. (Was: PAUSED by the user mid-SP2.) The SP2 implementer was stopped right after committing bc95e6a (A7 isolation;
  it reported 430 passed, ruff clean). The working tree is clean. On resume: re-dispatch SP2 for the remaining items (A6, B1,
  B2, B3, C4, B21, B22) with bc95e6a as the base, then run the phase review over f9d49b2..HEAD. No SP2 box is ticked yet.
  A partial report may be at .superpowers/sdd/sp-2-report.md.

---

## Audit appendix (source of finding IDs)

Four parallel read-only audits: backend process/sandbox, web/storage/security, frontend, product/pedagogy.
Deduplicated. "✔" = confirmed empirically (scratch harness / real run), else high-confidence code reading.
IDs are referenced by the plan.

## A. High — core promises broken

| ID | Where | Problem |
|---|---|---|
| A1 ✔ | `scripts/ensure-claude-auth.ps1:30` | BOM-less UTF-8 with an em dash; Windows PowerShell 5.1 reads it as cp1252 and **fails to parse**. The launcher ignores the exit code, so the v1.4 "friction-free auth" never runs. |
| A2 ✔ | `parsing.py:65-72` | `extract_code` returns the **first** tagged block. Guided (and often Answer) begins with teaching snippets, so the sandbox verifies a snippet and saves it as the `.py`: a correct Two Sum was graded 0/2. Indented (list-item) fences are also misparsed (`parsing.py:27-30`). |
| A3 ✔ | `sandbox.py:214-220` | Output compare only strips whitespace: `[0, 1]` vs `[0,1]`, `True` vs `true`, and quoting all FAIL. Your real `output/answers/hash_map/two_sum__normal.md` is recorded FAIL 0/3 even though it is correct. |
| A4 ✔ | `sandbox.py:404-409` | `**Input:**` markdown labels leave `**` in stdin/expected, giving a false FAIL. Multi-line inputs contradict the prompt's "read ONE line" contract (`prompts.py:177-180`). |
| A5 ✔ | `sandbox.py:275-293`, `proc_util.py:192-224` | Windows job-object caps (512 MB / process count) are assigned **after** spawn, and the venv launcher's grandchild can escape the job. A 700 MB allocation escaped the cap in 20/20 runs under GIL contention. |
| A6 ✔ | `claude_cli.py:216-222` | The watchdog won't fire if `claude` exits but a grandchild holds stdout, so the run hangs forever. `app.py:562` also joins the classifier thread with no timeout. |
| A7 ✔ | `claude_cli.py:166-191` | `claude -p` runs as a full Claude Code agent in the repo dir. It loads your plugins, hooks, skills, `CLAUDE.md` and the Concise output style (which shapes study docs). Every run is saved to your session history (19 of 35 saved sessions are LeetCoach runs), and tools can read `.env` if a pasted problem injects that. |
| A8 ✔ | `static/app.js:937-987` | Stats groups by stem before the first `__`: repeat days, tiers and `__2` collide, so the streak, heatmap and totals undercount (a 4-day simulation showed a streak of 1). `__2` is shown as the tier. `mdPath` is not the newest file. |

## B. Medium

| ID | Where | Problem |
|---|---|---|
| B1 | `claude_cli.py:82-92` | Auth probe: the timeout kills only `cmd.exe` (hangs 30 s ✔), cp1252 decode flips signed-in → signed-out for non-Latin names ✔, and it runs synchronously on **every** `GET /`. |
| B2 | `claude_cli.py:328-333` | Every nonzero exit is headlined "most likely signed out", including usage limits, a bad model and prompt-too-long. |
| B3 ✔ | `claude_cli.py:343-410` | The parser crashes on odd event shapes (`text: null`, non-dict). An `is_error` result with exit 0 gets saved as the answer. |
| B4 ✔ | `sandbox.py:595-600` | The error reason (timeout / crash note) is dropped. The user sees "errored 1/1" with no why. |
| B5 ✔ | `sandbox.py:596`, `config` | `int(timeout)`: a verify timeout of 0.5 becomes 0, and `inf` crashes the verifier or silently kills the run watchdog. |
| B6 ✔ | `sandbox.py:171-175, 392` | Buffered text read loses stdout when a grandchild holds the pipe, and `rmtree` races the kill, leaking temp dirs (98 stale `leetcoach_run_*` in %TEMP%). |
| B7 ✔ | `topic_index.py:98-147` | `os.replace` fails with PermissionError on Windows when the file is open (AV/OneDrive/reader), so updates are silently lost (17 of 300 persisted) and `.tmp` files are left. A corrupt index is silently overwritten. |
| B8 ✔ | tests + `app.py:54` | The test suite loads the real `.env` and writes fake topics into your real `output/topic_index.json`. |
| B9 | `setup.ps1`, `LeetCoach.cmd` | A failed `pip install` still prints "Setup complete" and is never retried, and the launcher window closes with no pause. The auth login would block inside a minimized window. |
| B10 ✔ | `app.py:322-341` | The library cache misses nested external adds/deletes, which show as stale entries that 404. |
| B11 | `app.py:736` | A second launch starts a second server on :5001 sharing `output/`. The per-process locks don't coordinate. |
| B12 ✔ | `config.py:193-208`, `app.js:274-280` | Model picker: a UTF-16 `.env` gives a 500 (and a crash at boot), a duplicate key loses the choice, a read error wipes `.env`, the write isn't atomic, the UI ignores `resp.ok`, and the model is global across tabs rather than sent per run. |
| B13 | `app.js:548-561, 789-809` | Shared global run state: an old run's `finally` can clobber a new run's Stop and streaming flags. |
| B14 | `app.js:742`, `app.py:482` | After Stop, re-running the same settings gets a 409 "already in progress" until the server notices the disconnect (no heartbeat). |
| B15 | `app.js` | Reload loses the draft problem, mode, language and tier (no localStorage), and there's no `beforeunload` guard mid-run. |
| B16 | `app.js:368-395` | Every frame re-parses the whole doc and re-highlights all code (plus auto-detection on untagged blocks). This causes 20–30 KB docs to jank, wipes text selection, and makes copy buttons flicker. |
| B17 | `app.js:802-805` | A stream that closes without `done` shows "Saved · 0 files" (false success). |
| B18 | `app.js:1056` | The recents/table status column always shows a green ✓, even on a FAIL verdict. The FAIL/not-verified chips aren't colored. |
| B19 | `app.js:1589-1608`, `app.py:80` | Delete removes only the `.md` (orphaned `.py` already in your library). `topic_index.json` is listed, searchable and deletable. |
| B20 | `app.js:863-906` | Quick Ask can't be cancelled and can hang for 10 min. |
| B21 | `classifier.py`, `prompts.py:104`, `topic_index` | Classifier `problem_type` is free-form, which fragments folders (greedy vs two_pointers, dp vs dynamic_programming). Topics are unbounded: a 4 KB topic was accepted and replays into every Learning prompt (persistent injection). The brace-counting JSON extract is broken ✔. The `--- END PROBLEM ---` delimiter can be forged. |
| B22 | `prompts.py`, `app.py:615` | Guided says "already-learned: none" and never records topics. The topic index ignores language. Learning leaks full solutions (real output confirms). |
| B23 ✔ | `app.py:452,681,397` | A non-object JSON body (list, string) gives a 500 instead of a 400. |
| B24 ✔ | `app.py:576-597` | Answer with no code block saves a 0-byte `.py`. |
| B25 ✔ | `storage.py:47,92` | Non-ASCII titles all become `untitled` and collide. A save OSError (long path / disk) discards a fully streamed answer. |

## C. Low / hardening / polish

- C1 ✔ No `frame-ancestors` / `X-Frame-Options` → clickjackable Run/Delete/model picker.
- C2 No Origin / `Sec-Fetch-Site` check on unsafe methods (safe today only because of the JSON content type).
- C3 No SSE heartbeat, so a disconnect goes unnoticed during long thinking (ties to B14).
- C4 A disconnect during a large stdin write raises a spurious ClaudeUnavailableError in close (`claude_cli.py:320`).
- C5 POSIX tree-kill only terminates the direct child.
- C6 Sandbox has no file/network isolation (documented in SECURITY.md). An audit-hook bootstrap would block careless or injected code from reading `~/.claude/.credentials.json` / `.env`.
- C7 `.env.example` pins relative `output` paths, undoing the absolute default. It says "eight" settings and omits `LEETCOACH_NO_BROWSER`.
- C8 `.gitattributes` gives `.cmd` files LF endings (fragile for cmd.exe). The README tells users to run `.ps1` scripts that the default execution policy blocks.
- C9 a11y: `aria-live` on the node replaced every frame; recents rows are mouse-only divs; topic chips look clickable but do nothing; segmented controls have no `aria-pressed`; `--tx4` contrast is 2.8:1; no focus trap in modals; missing labels on `#problem` / `#qa-input`; IME Enter submits.
- C10 UX: Ctrl+Enter from Library/Stats runs invisibly; library viewer doesn't reset scroll or keep the active-file highlight; views share one scroll container; "Copied" label sticks; ⌘ glyphs on Windows; fake line-number gutter; silent failures (paste, openFile, model save); the summary title reads the live textarea; Stats flashes the empty state on boot; `&` autolinks get double-escaped.
- C11 Layout: below 900 px the sidebar is hidden with no replacement; the topbar overflows; dark theme only.
- C12 Dead CSS and variables; the Diff column is always "—".
- C13 Docs drift: README/CHANGELOG auth claims (A1); ARCHITECTURE says "read-only library" and omits DELETE and model routes; the Quick Ask "haiku" tag is hardcoded; there's a stale comment at `app.py:91`.
- C14 CI: no `.ps1` parse check (how A1 shipped), no JS tests, no Python 3.14 job (the local venv is 3.14).

## D. Feature opportunities (ranked; ★ = top 5)

| # | Feature | Value | Effort |
|---|---|---|---|
| ★D1 | **Problem record + run log**: save `problem.md` + `meta.json` (number, title, difficulty parsed from the paste, fixed pattern, model, verdict) and an append-only `output/.runs.jsonl`. Stats is computed from the log (fixes A8 properly); the Diff column becomes real. | H | M |
| ★D2 | **Study-doc contract + hint ladder**: every mode gets a fixed structure (constraints → target complexity, how to recognize the pattern, key insight, brute → optimal, edge cases, pitfalls, related problems, flashcards). No trailing questions, one tagged solution block. Guided gets `### Hint 1..4`, rendered click-to-reveal. | H | S–M |
| ★D3 | **Re-attempt + "Test my code"**: re-open a saved problem with the answer hidden, write code, and run it in the sandbox against the samples and your own cases (Python). Reveal on give-up. | H | M |
| ★D4 | **Spaced-repetition review queue**: self-grade after an attempt (solo / hints / peeked). Leitner schedule 1/3/7/14/30 days, with "N due today" on the Console. | H | M |
| ★D5 | **Code Review mode**: paste your attempt; Claude critiques bugs, complexity and edge cases without rewriting it. Saved to the library. | H | S |
| D6 | Follow-up chat on a saved doc via `claude --resume` (conflicts with the A7 no-persistence fix unless per-run opt-in) | H | M |
| D7 | Persist workspace (draft, mode, language, tier) + summary action buttons ("Open in Library", "Re-run as Optimal / in C++", "Learn this topic") | M | S |
| D8 | Progress by pattern: weakest-pattern view + "try next" (needs the fixed pattern list) | M | M |
| D9 | Notes per problem (`notes.md` in the viewer) | M | S |
| D10 | Flashcards from the `## Flashcards` section: in-app review + Anki TSV export | M | S |
| D11 | Stream UX: auto-follow + "jump to latest", phase events (streaming → verifying 2/3 → saving) | M | S–M |
| D12 | Attempt timer / mock-interview mode | M | S–M |
| D13 | Harness-based verification (`class Solution` + app-side input parsing), which makes saved code pasteable into LeetCode | H | M–L |
| D14 | C++/Java compile-and-run verification (backlog P2-10) | M | L |
| D15 | Light theme + responsive layout (C11) | L–M | M |
| D16 | Single-instance launcher (reuse the running server; B11) | M | S |
| — | Fetch by LeetCode URL/number: **conflicts with the offline/no-egress stance**; paste clean-up (10^4 flattened to 104, header line) is the compliant alternative. | | |

## E. Backlog hygiene (cycle-10 BACKLOG, now reset)

- The ⌘K palette is done. The "run-history store" item is superseded by D1. The difficulty item's premise is stale (it can be parsed from the paste).
- ruff 0.16 was mis-filed under "Done" (it is not done) and duplicated. The package-restructure item pointed at an empty DECISIONS "Open".
