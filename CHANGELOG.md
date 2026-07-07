# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
