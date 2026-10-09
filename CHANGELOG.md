# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] - 2026-10-08

The study-loop release. Every audit finding is fixed (A1-A8, B1-B25, C1-C14), and the app
grows from "generate a doc" into a practice loop: problem records, a run log, hints,
re-attempts with "Test my code", a review queue, Code Review mode, notes, flashcards, and
follow-up questions on a saved doc.

### Added
- **Problem records and a run log (D1).** Each saved run records its problem under
  `output/.leetcoach/problems/` (number, title and difficulty parsed from the paste,
  pattern, statement) and appends one line to `output/.leetcoach/runs.jsonl`. The library
  table's Diff column now shows real data, and `GET /problems` / `/problems/<id>` expose
  the records. The library, search and delete all hide the metadata folder.
- **A study-doc contract and a hint ladder (D2).** Every mode follows one structure:
  problem in brief, target complexity, how to recognise the pattern, key insight,
  approach, solution, complexity, edge cases, common mistakes, related problems,
  flashcards. Each doc tags exactly one ```` ```<lang> solution```` block. Guided and
  Learning add `### Hint 1..4`, shown click-to-reveal, and Learning never contains a full
  solution. Problems are filed under a fixed list of about 18 patterns.
- **Re-attempt and "Test my code" (D3).** Re-open a saved problem with the answer hidden,
  write your own Python, and run it in the sandbox against the problem's samples and your
  own cases (`POST /attempt/test`, cancellable). Give up to reveal the latest doc. C++ and
  Java say clearly that they are not supported yet.
- **A spaced-repetition review queue (D4).** Grade each attempt as solo, with hints, or
  peeked. A Leitner schedule (1, 3, 7, 14, 30 days) moves the due date. The Console shows
  "Due today (N)" and Stats shows review counts.
- **Code Review mode (D5).** Paste your own attempt and Claude critiques its bugs,
  complexity and edge cases without rewriting it. Reviews are saved under
  `output/reviews/`.
- **Follow-up questions on a saved doc (D6).** Ask a question in the library viewer. The
  answer streams in and is then appended to the doc under `## Follow-up — <question>`.
  When it can, LeetCoach resumes the run's own Claude session (`claude --resume`);
  otherwise it makes a fresh, isolated call with the doc as context. Stop or `Esc`
  cancels, and nothing is added unless the answer completes. Follow-ups never change the
  doc's verdict and do not count as runs in Stats.
- **Workspace persistence and summary actions (D7).** The draft problem, mode, language
  and Code Quality level survive a reload, and leaving mid-run asks first. A finished run
  offers Open in Library, Re-run as Optimal, Re-run in another language, and Learn this
  topic.
- **Notes per problem (D9).** Edit them in the library viewer; they are saved in the
  problem record.
- **Flashcards (D10).** Cards are parsed from each doc's `## Flashcards` section, with an
  in-app flip review and an Anki export (`GET /flashcards.tsv`).
- **Stream UX (D11).** The view follows the stream and shows a "Jump to latest" pill when
  you scroll away. It also shows the progress phase (streaming, verifying i/n, saving)
  and the concrete model that answered.
- **Single instance (D16).** `GET /healthz` identifies LeetCoach. A second launch opens the
  browser on the running app instead of starting another server on another port.
- **`LEETCOACH_CLAUDE_CWD`**, the neutral folder every `claude` call runs in.

### Changed
- **Isolated Claude calls (A7).** `claude -p` no longer runs as a full agent in the repo.
  - Every call gets `--safe-mode`, `--tools ""`, `--strict-mcp-config` and a
    `--system-prompt` persona, each only when the installed CLI lists it.
  - Every call runs in the neutral `LEETCOACH_CLAUDE_CWD`.
  - `--bare` is never passed, because it drops the subscription login.
  - Study runs keep their session so follow-ups can resume it. The classifier, Quick Ask
    and the follow-up fallback use `--no-session-persistence`. The sign-in probe is
    `claude auth status`, which makes no model call and creates no session.
- **Stats come from the run log (A8).** Libraries saved before the log still count, one
  activity per saved run at its own date, so streaks, the heatmap and the totals no longer
  undercount. The server now computes Stats (`GET /stats`).
- **Safer prompts (B21, B22).** Pasted problem text is fenced with random delimiters
  generated for each prompt. Topics are sanitised and capped, and the topic index is
  keyed by language.
- **Quick Ask model tag (C13).** The page now shows the configured
  `LEETCOACH_QUICK_ASK_MODEL` instead of a hard-coded "haiku".
- **Dependencies.**
  - The vendored Markdown renderer moves from marked 12.0.2 to 18.1.0. The hardened
    renderer (no raw HTML, http(s) links only, `data:image/` images only) is ported to
    marked's token API, and new tests pin its behaviour.
  - highlight.js moves from 11.9.0 to 11.12.0. CREDITS.md lists both versions, and a test
    keeps it in step with the vendored files.
  - `requirements.lock` is regenerated from a clean resolve and passes `pip check`. Python
    3.14 is the pinned development version; 3.12 is still the minimum.
- **Lint.** Ruff moves to 0.16 with its larger default rule set, and every finding is
  fixed in code. The only `noqa` comments left each give a reason.

### Fixed
- **Deep memoized recursion on Python 3.12/3.13 is explained.** Those versions cap
  recursion through `functools.cache` / `lru_cache` at a fixed C-level depth (about 1000
  levels on Windows) that `sys.setrecursionlimit` does not raise. A sample that dies with
  a `RecursionError` there now says so in its verdict note; Python 3.14 has no such cap.
- **Ship audit: launch and setup.**
  - `setup.ps1` no longer aborts under Windows PowerShell 5.1 when a native command
    writes to stderr. A `.venv` missing packages is installed into, and one whose Python
    is missing or cannot start is recreated with `venv --clear`.
  - `LeetCoach.cmd` works from a folder whose path contains `!`.
  - `ensure-claude-auth.ps1` bounds both waits on the CLI: a hung `auth status` is killed
    after 15 s, and the app starts after 5 minutes even if the sign-in window is still
    open (`LEETCOACH_AUTH_STATUS_TIMEOUT_MS` / `LEETCOACH_AUTH_LOGIN_TIMEOUT_MS`).
  - A `.env` that is not UTF-8 (for example saved as UTF-16 by Notepad) no longer stops
    the app from starting: Flask no longer loads `.env` a second time on its own.
  - Launching no longer crashes when a non-HTTP service listens on a port in the
    single-instance probe range.
  - A blank `LEETCOACH_MODEL`, `LEETCOACH_CLASSIFIER_MODEL`, `LEETCOACH_QUICK_ASK_MODEL`
    or `LEETCOACH_CLAUDE_BIN` counts as unset.
- **Ship audit: requests and errors.**
  - A body that is not JSON now gets "Request body must be a JSON object." instead of a
    misleading "Problem text is required.". A deeply nested body gets the same JSON 400,
    and a body over 2 MB gets a JSON 413 that Run and Quick Ask display.
  - "Test my code" returns an actionable JSON error when the sandbox cannot start.
  - An expected CLI failure (not installed, signed out, failed to start) is logged as one
    line instead of a traceback; unexpected errors keep theirs.
  - Multi-line CLI errors no longer repeat their first line. A failed `claude --help`
    probe logs why and is retried once on timeout. A cancelled classification is logged
    at DEBUG.
- **Ship audit: records and files.**
  - A problem record or run-log line with a wrongly typed field no longer breaks
    `/problems`, `/review`, `/stats`, grading or later saves. Bad fields are dropped, and
    the original is kept as `<name>.corrupt-<ts>` before the repaired record is written.
  - A record that briefly cannot be read (antivirus or OneDrive holding it) is skipped,
    not replaced. On Linux and macOS the record lock gives up after 10 s, as on Windows.
  - A rollback never deletes a file it could not back up.
  - CRLF text is saved with single line breaks, so an identical re-run reuses its file.
  - A paste whose first line is generic ("Description", "Problem:") is saved under the
    problem's title instead of `description.md`, `description__2.md` and so on.
- **Ship audit: runner and sandbox.**
  - On Linux and macOS, helper processes the CLI leaves behind are killed with its
    process group when a run ends.
  - The neutral working folder no longer fails a run when no home folder resolves;
    `LEETCOACH_CLAUDE_CWD` expands `~`, and the temp fallback is made once per process.
  - An explicit `uncategorized` classification is kept. A deeply nested stdout line no
    longer crashes the runner, and an emoji split across two stream deltas is rejoined.
  - A sandbox that cannot create its run folder reports `not_verified` with the reason.
- **Ship audit: front end.** The library viewer ignores out-of-order responses, so
  opening A then B always shows B. Quick Ask sends only the 6,000 characters of the
  problem the server reads. Unused helpers are gone from `static/lib/core.js`.
- **Classifier failures are logged.** When the save-time classifier call fails, a warning
  with the error is logged before the fallback pattern is used, so a persistent failure
  can be diagnosed.
- **Launch and sign-in (A1, B9).** `ensure-claude-auth.ps1` is now ASCII-only, so it
  parses under Windows PowerShell 5.1. The desktop shortcut's automatic sign-in now
  actually runs, in its own visible window. `setup.ps1` checks every step's exit code,
  and the launcher pauses on failure.
- **Verification correctness (A2-A4, B4, B5, B24).**
  - The verifier picks the tagged solution block, not the first teaching snippet.
  - Output is compared structurally: `[0, 1]` equals `[0,1]`, and `True` equals `true`.
  - Markdown is stripped from sample labels.
  - The reason for each failed or errored sample is kept.
  - Timeouts can be fractional.
  - An answer with no code block no longer writes an empty code file.
- **Process control (A5, A6, B1, B3, B6, C4, C5).**
  - The memory and process caps apply before any generated code runs.
  - A run no longer hangs when `claude` exits while a grandchild still holds its pipe.
  - The sign-in probe can time out, decodes UTF-8, and is cached.
  - Odd stream events no longer crash the parser.
  - Output is captured with byte caps, and stale sandbox folders are cleaned up.
- **Error messages (B2).** A failed run's headline comes from the CLI's real error.
  "Signed out" appears only when the CLI reports it.
- **Storage (B7, B10, B12, B19, B25).**
  - Writes are atomic and retry while another process holds the file.
  - A UTF-16 or duplicate-key `.env` no longer breaks the model picker.
  - Non-ASCII titles get distinct file names.
  - A failed save keeps the answer under `output/_unsorted/`.
  - Delete removes a whole run.
  - The library notices nested changes, and the topic index is hidden from it.
- **Front end (B13-B18, B20).**
  - Run state is guarded by a run id, so Run works immediately after Stop.
  - Rendering is throttled so large docs do not jank.
  - A cut stream is reported as an error, never as "Saved".
  - Status columns and verdict chips show FAIL.
  - Quick Ask can be cancelled.
- **Request handling and config (B23, B8, C7).** A non-object JSON body returns 400
  instead of 500. The test suite never reads your real `.env` or writes your real topic
  index. `.env.example` lists every setting.
- **Accessibility, narrow screens and polish (C9-C12).** Live regions, keyboard-reachable
  rows, focus traps, better contrast, a sidebar drawer below 900 px, and dead CSS removed.

### Security
- **`multiprocessing` can no longer start an unhooked child on Linux or macOS.**
  `_posixsubprocess.fork_exec` raises no audit event, and `multiprocessing`'s spawn
  start method (the macOS default) and its resource tracker call it directly, so a
  solution could start a Python child without the sandbox's audit hook. The bootstrap
  now replaces it with a refusal before the solution runs.
- **The POSIX process limit no longer breaks threads on a busy desktop.** `RLIMIT_NPROC`
  counts every process of the user (every thread on Linux), so the fixed cap of 64
  stopped a solution from starting even one thread once the desktop ran more than that.
  It is now the user's current count plus 128 (from `/proc` on Linux, `ps` elsewhere),
  and is left unset when that count cannot be taken.
- **`claude` never sees an API key.** Child processes no longer inherit
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK` or
  `CLAUDE_CODE_USE_VERTEX`, so every call uses the Claude Code subscription instead of
  billed API credits. A one-time warning names any variable it withheld.
- **Neutral folder fallback is per user.** The temp-folder fallback is
  `leetcoach-claude-cwd-<uid>`, created `0o700` on POSIX, and one owned by another user is
  refused. `taskkill` runs from `%SystemRoot%\System32`, never a bare name.
- **Library containment.** The library listing, verdicts and flashcard export skip
  symlinks and junctions inside `output/` that point outside it.
- **More secret paths.** The audit hook also denies `~/.npmrc`, `~/.pypirc`,
  `~/.docker/config.json`, `~/.kube` and `~/.gnupg`.
- **Sandbox start-up (A5).** The sandbox child is the real interpreter running a trusted
  bootstrap. The bootstrap waits for a go byte that is sent only after the Job Object is
  assigned, and the sandbox fails closed.
- **Audit hook (C6).** It blocks writes outside the run folder, reads of known secret
  paths, all sockets, SQLite, process creation, `ctypes` and heap walking. It is defence
  in depth, not a boundary; see SECURITY.md.
- **Web hardening (C1-C3).**
  - `frame-ancestors 'none'` and `X-Frame-Options: DENY` stop clickjacking.
  - State-changing requests from another origin are refused.
  - SSE streams send a heartbeat, and runs can be cancelled on the server.
- **Anki export.** Cells that a spreadsheet would treat as formulas are neutralised.

### Notes
- **CI and tests (C14).** CI parses every `.ps1` under Windows PowerShell 5.1. The
  JavaScript helpers have zero-dependency node tests, which pytest runs.
  - CI now runs on every push to any branch. It tests Windows on Python 3.12-3.14,
    Ubuntu on 3.12 and 3.14, and macOS on 3.14, and runs the node tests in each job.
  - Two clean-runner jobs (Windows and Linux) follow the README's setup steps literally
    against the fake `claude` CLI, and check that `/healthz` and `/` return 200.
- **Line endings and encodings.** `.gitattributes` names every text type the repo
  ships (LF, with CRLF for `.cmd`/`.bat`/`.ps1`) and marks media as binary. A test fails
  on any tracked Python file that opens text without `encoding=`, and another fails on
  any `LEETCOACH_*` variable missing from `.env.example`.
- **Tests no longer depend on the clock.** Review and Stats tests freeze "now", so they
  pass across midnight and DST changes.
- **Docs (C13).** README, ARCHITECTURE, SECURITY and this changelog are up to date.
- **Existing libraries.** Existing `output/` libraries keep working with no migration:
  legacy files count in Stats and open in the viewer. Follow-ups on them use the fallback
  path.

## [1.4.0] - Unreleased

### Changed
- **The default model now tracks the latest Opus** through the `opus` alias instead of
  a pinned id, and **Fable** (the most capable model) is added to the model picker. The
  picker tooltips show the current version of each model; the aliases themselves are
  what reach `claude --model`.

### Added
- **One-click desktop launch.** A `LeetCoach.cmd` launcher plus
  `scripts/create-shortcut.ps1`, which drops a `LeetCoach` shortcut on your Desktop
  (resolving a OneDrive-redirected Desktop), so a double-click starts the app and
  opens it in your browser. The first run bootstraps the virtual environment through
  `setup.ps1` if it is missing. The shortcut and browser tab get a real `[lc]` app icon.
- **The app opens your browser on start**, and when the default port is already in
  use it falls back to the next free port instead of failing to launch. Set
  `LEETCOACH_NO_BROWSER=1` to suppress the auto-open for headless or dev use.
- **A Stats tab.** Your saved runs now drive a practice dashboard: current and longest
  daily streak, solved today, this week, and in total, a calendar heatmap of the last
  several months, and a breakdown by mode, language, and topic. A streak badge sits in
  the Console header. Everything is derived from the files already in `output/`, so it
  stays offline and adds no new storage.
- **A search palette** (`Ctrl`/`Cmd` + `K`) that filters your saved runs and library by
  problem, topic, mode, or language and opens the file you pick.
- **A keyboard-shortcuts dialog** (press `?`) listing what the app responds to. Pressing
  `?` while typing in the problem box does nothing, as expected.
- **An in-app model picker** in the Console (Opus / Sonnet / Haiku). Your choice is saved
  to `.env` and applied to the very next run with no restart, so you can switch to a
  stronger or cheaper model whenever the mood strikes. Quick Ask and the topic classifier
  keep their own Haiku defaults.
- **The desktop shortcut signs you in when needed.** Before starting the app, the launcher
  checks the `claude` CLI's sign-in state and, if you are signed out, runs
  `claude auth login` for you — so a run never fails on an expired session. *(In 1.4.0
  the sign-in script did not parse under Windows PowerShell 5.1, so this step silently
  did nothing. Fixed in 1.5.0; see A1.)*

### Changed
- Renamed the **Tier** control to **Code Quality**, with clearer levels **Basic / Normal /
  Optimal** (previously Simple / Normal / Complex). Existing saved answers are renamed to
  match on startup — a one-time, idempotent migration that never overwrites a file.
- Removed two placeholder controls that never did anything: the sidebar Bookmarks entry
  and the recent-runs Clear link.

### Fixed
- A failed run now says *why*, and how to fix it. `claude` reports an expired login (and
  other API errors) on stdout as stream-json, which the app discarded — leaving a bare
  "exited with code 1." The run-failed message now surfaces the real reason (e.g. "Failed
  to authenticate: OAuth session expired…") and names the exact fix, `claude auth login`.
  The startup banner likewise tells signed-out and not-installed apart and shows the
  copy-paste command.

### Notes
- Runs work exactly as before: the `claude` CLI dependency, the SSE streaming pipeline,
  the Answer-mode sandbox, the `output/` storage layout, and the `/run` request contract
  are all unchanged. The Stats tab and the search palette read only your existing library,
  with no new endpoint. The suite is now 315 tests, still mocking the subprocess.

## [1.3.3] - 2026-08-26

### Internal
- Stopped a Windows sandbox test from flaking. The fork-bomb process-cap test
  asserted one of two valid timing outcomes (the child self-detecting the full
  job and exiting with a marker code); on a loaded machine the job tears the
  child down first, so the test failed roughly two runs in three and reddened CI
  at random. It now asserts the property that holds every time: a working cap
  means the bomb never spawns all its children, so the run never passes.
- Bumped click to 8.5.0 in the lockfile and cleared a pip advisory (PYSEC-2026-3721)
  in the local toolchain.

## [1.3.2] - 2026-08-19

### Fixed
- README setup now activates the virtual environment before running the app.
  Following the steps verbatim on a clean clone previously ran the system Python
  and failed with a missing-Flask error.

### Changed
- Refreshed the README screenshot and demo GIF. Both predated the v1.3.1 logo fix
  and still showed the old stacked `[lc]` mark; they now match the current UI
  (and show the per-file Delete button added in v1.3.0).
- Added a Limitations section to the README: single-user localhost, Python-only
  answer verification, the sandbox as a convenience rather than a security
  boundary, and the `claude` CLI dependency.

### Internal
- Pinned Pygments, python-dotenv, and packaging to current patch releases, and
  capped ruff below 0.16 so CI installs a linter matching the lockfile.
- Added coverage for the NaN branch of the run and verify timeout settings.

## [1.3.1] - 2026-07-21

### Fixed
- The `[lc]` header logo stacked its brackets and letters vertically. The badge
  used a CSS grid whose default row flow put each span on its own line; it now
  lays them out inline so the mark reads `[ lc ]`.

## [1.3.0] - 2026-07-20

### Added
- **Quick Ask**: an inline box for a short syntax, standard-library, or concept
  question, answered by a cheap model (Haiku by default) without leaving the page.
  It refuses to solve the problem you are composing, so it stays a lookup helper.
  Backed by a new `POST /ask` endpoint and the `LEETCOACH_QUICK_ASK_MODEL` setting.
- **Delete a library file** from the Library viewer. A per-file Delete button
  removes one saved file; the new `DELETE /library/file` endpoint enforces the
  same path containment as the read endpoint (no traversal, no mass-delete).
- `LEETCOACH_VERIFY_TIMEOUT` (default 10s) caps each Answer-mode sample
  verification independently of the overall run timeout.
- Security headers on every response: a strict Content-Security-Policy and
  `X-Content-Type-Options: nosniff`.

### Changed
- `/run` now rejects an oversized request body with 413, and a duplicate
  in-flight run (same problem, mode, language, tier) with 409 instead of fanning
  out concurrent Claude calls.
- `/run` and `/ask` return 400, not 500, when a JSON field is the wrong type.
- The `/library` listing is cached behind a freshness key, so opening the Library
  tab or refreshing after a run no longer re-walks the whole tree each time.
- The markdown renderer no longer auto-loads remote images (alt text only for
  non-inline image sources), closing an egress channel.

### Fixed
- A large prompt could deadlock the `claude` subprocess when its output filled
  the OS pipe before it finished reading stdin; stdin is now written on its own
  thread while stdout drains.
- Concurrent saves of distinct answers for the same problem no longer overwrite
  each other (storage writes are serialized behind a lock).
- Code extraction no longer truncates at a nested triple-backtick inside a fenced
  block; the closing fence is anchored to the start of a line.
- A garbage `problem_type` from the classifier now falls back to the
  `uncategorized` bucket instead of a stray `untitled` one.
- Answer verification distinguishes an errored sample from a wrong-answer sample
  in the reported status and note.

### Notes
- The `claude` CLI dependency, the SSE streaming pipeline, the Answer-mode
  sandbox, the `output/` storage layout, and the `/run` request contract are all
  unchanged. The suite is now 281 tests, still mocking the subprocess.

## [1.2.0] - 2026-07-14

### Changed
- **UI restructured into an application shell.** The single-column page became a fixed
  top bar, a left sidebar, and a fluid content column with two views: a **Console** (paste,
  configure, run, and watch the answer stream) and a **Library** (browse everything saved
  under `output/`). Mode, language, and tier are now segmented button controls.
- The run header shows a live elapsed timer, the run's mode/language/tier chips, and the
  Stop button while a run streams; a caret marks the live stream; finishing renders a
  summary card with the problem type, topics, verification result, and saved-file paths.
- The Console sidebar's recent-runs list, the recent-runs table, and the topic strip are
  derived live from your real `output/` library. Difficulty renders neutral (the tool has
  no LeetCode difficulty signal).

### Added
- `GET /library` now includes each file's modification time (`mtime`) so the UI can show
  real saved dates. Backward-compatible additive field.

### Notes
- Runs work exactly as before: the `claude` CLI dependency, the SSE streaming pipeline,
  Answer-mode sandbox verification, the `output/` storage layout, and the `/run` request
  contract are all unchanged. The suite stays at 224 tests, still mocking the subprocess.

## [1.1.0] - 2026-07-10

### Added
- A **Stop** button that cancels a run mid-stream (and stops the `claude`
  subprocess, so an abandoned run spends no further subscription budget).
- A read-only **library browser**: a Library panel in the UI backed by
  `GET /library` (listing) and `GET /library/file` (one file's raw text, served
  as `text/plain` and rendered through the same hardened markdown pipeline as
  run output).
- Failed verification runs now save per-sample detail (input, expected, actual
  output, stderr, exit code) into the reasoning `.md`, so a FAIL is debuggable
  after the fact.
- Two new env knobs: `LEETCOACH_RUN_TIMEOUT` (wall-clock cap in seconds for a
  single `claude` run, default 600) and `LEETCOACH_CLASSIFIER_MODEL` (model for
  the short classification call, default `haiku`).

### Fixed
- Sample-I/O parser: multi-line `Input:` / `Output:` bodies are now captured in
  full instead of only their first line, which could produce false FAIL (or
  false PASS) verdicts on multi-line examples.
- A run whose stream produced no text at all is now reported as an error instead
  of being saved as an empty success.
- A `claude` startup failure (BrokenPipe on stdin) now surfaces the CLI's real
  stderr instead of masking it with the pipe error.
- A hung `claude` CLI (network stall, stuck auth prompt) no longer wedges the
  run forever: a wall-clock watchdog kills the process tree after
  `LEETCOACH_RUN_TIMEOUT`.
- Launching via `flask run` (or any non-project working directory) no longer
  forks a second study library: the default output dir is now anchored next to
  the app (see **Changed**). Colliding filenames get a `__2` / `__3` suffix
  instead of silently overwriting earlier notes; identical re-runs stay
  idempotent.
- Removed the dead syntax-highlight option in the frontend and coalesced
  markdown re-renders onto animation frames, so long answers stream without
  jank.

### Security
- Every request's `Host` header is checked against a loopback allowlist and
  rejected with a 403 otherwise, blocking DNS-rebinding pages from driving the
  app through the browser.
- The verification sandbox on Windows now runs the child inside a Job Object
  (512 MB per-process memory cap, 16 active-process cap, kill-on-job-close),
  kills the whole process tree on timeout, and bounds captured output at 64 KB
  per stream while reading. See SECURITY.md for what it still does not confine.
- Links in rendered markdown are restricted to safe protocols, neutralizing
  `javascript:` URLs in model output.

### Changed
- **Default output directory moved.** The study library now defaults to the
  `output` directory next to the app instead of the current working directory.
  `python app.py` from the project root is unaffected; if you used `flask run`
  from elsewhere, your existing notes are wherever that CWD was: move them
  into the app's `output/` or point `LEETCOACH_OUTPUT_DIR` at them.
- Classification now runs concurrently with the answer stream on a cheap model
  (`haiku` by default), so it no longer delays the first streamed token.
- The Learning prompt interpolates at most the 50 most recent learned topics,
  keeping prompt size bounded as the index grows.
- Test suite grown to 224, all still mocking the `claude` subprocess.

## [1.0.2] - 2026-07-07

### Fixed
- `topic_index`: fixed a TOCTOU race in `record()` so concurrent runs no longer
  clobber each other's entries.
- SSE streaming: a mid-stream `claude` failure is now delivered to the browser as
  an explicit SSE error event instead of silently truncating the response.
- `claude_cli`: the `claude` subprocess is now cancelled deterministically when the
  SSE client disconnects (with a Windows process-tree kill), so an abandoned run no
  longer keeps burning subscription usage.

### Security
- Rendered markdown now escapes any raw HTML in Claude's output (defense-in-depth
  XSS hardening for the local UI).

### Changed
- Sample-I/O parser now handles multi-line `Input:` blocks.
- Test suite grown to 154, all still mocking the `claude` subprocess.

## [1.0.1] - 2026-06-29

### Fixed
- `claude_cli`: the child process's stderr now goes to a temp file instead of an
  unread pipe. A large stderr burst from `claude` could previously fill the OS pipe
  buffer and deadlock the streaming read (child blocked writing stderr, parent blocked
  reading stdout). Added two hermetic regression tests, bringing the suite to 145.

## [1.0.0] - 2026-06-28

First public release.

### Added
- Three study modes driven by the `claude` CLI (no API key; uses a Claude Code
  subscription): **Learning** (teach the full stack a problem needs), **Guided
  Learning** (restate, teach, reason, then answer in one document), and **Answer**
  (a working solution with reasoning and an explicit Big-O line).
- Live streaming of Claude's response to the browser over server-sent events.
- Three answer tiers for Answer/Guided: *simple*, *normal*, *complex*.
- C++, Java, and Python prompt templates.
- Sample-I/O verification: generated Python solutions are run against the problem's
  own `Input:`/`Output:` examples in a throwaway, secret-free sandbox and reported as
  PASS / FAIL (C++/Java are marked "not auto-verified").
- A topic index so Learning skips and cross-links material you have already studied.
- Always-on time/space complexity annotation for every solution.
- A growing `output/` study library, organized by problem type.
- Single-page dark UI with paste-from-clipboard, vendored markdown rendering and
  syntax highlighting (no runtime CDN).
- 143 tests, all mocking the `claude` subprocess (no real Claude calls in the suite).
