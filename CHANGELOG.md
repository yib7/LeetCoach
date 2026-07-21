# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
