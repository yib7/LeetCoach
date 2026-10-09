# Security

## Scope and threat model

LeetCoach is a **single-user tool that runs on `localhost`** and drives your own
authenticated `claude` CLI. It has no accounts and no login. It holds no API keys or
secrets: it uses your Claude Code subscription through the CLI.

Everything it stores lives under `output/`:

- the study docs you generate;
- per-problem records (statement, notes, review schedule) and a run log, in the hidden
  `output/.leetcoach/` folder;
- the topic index.

These parts handle untrusted input, and each is deliberately contained:

- **Pasted problem text, your own code (Code Review) and follow-up questions** flow only
  into prompts and into filename generation.
  - Prompts are sent to `claude` on stdin, never on the command line. The text is fenced
    with random delimiters generated for each prompt, so it cannot close its own fence.
  - Filenames go through a single `slug()` function, which strips path separators and
    `..` so a write can never escape `output/`.
  - A library path named in a request must resolve inside `output/` and not be
    dot-prefixed, so the app's metadata is never served or deleted.
- **Answer / Guided verification** runs the Python solution Claude generated, to check
  it against the problem's own sample I/O.
- **"Test my code" (`POST /attempt/test`)** runs the Python you type in the re-attempt
  view against the samples and your own cases.
  - It runs **only** through the same sandbox: there is no other code path that executes
    submitted code.
  - Only Python is accepted.
  - One test runs at a time, and it can be cancelled.

  Both kinds of run use a throwaway directory, a secret-free environment, resource
  caps, an audit hook and a wall-clock timeout, as described below.
- **Claude's output** (study docs, follow-up answers, Quick Ask replies) is rendered in
  the page through a hardened markdown renderer. Model-derived interface elements are
  built with `textContent`, never `innerHTML`, under a strict CSP (see
  [Keep it local](#keep-it-local)).

### The sample-I/O sandbox is a convenience check, not a security boundary

Verification executes model-generated code on your machine. The isolation is
best-effort. What the sandbox does:

- a throwaway working directory, deleted afterwards;
- an environment scrubbed of your variables (no API keys, tokens, or app secrets
  reach the child);
- a wall-clock timeout that kills the **whole process tree** on expiry, so
  grandchildren spawned by the generated code die too;
- captured output bounded at 64 KB per stream, enforced while reading: exceeding
  the cap kills the tree instead of buffering it;
- on Windows, the child runs inside a **Job Object** that caps per-process memory
  at 512 MB and the tree at 16 active processes, with kill-on-job-close so nothing
  survives the run. The child is the real Python interpreter (not the venv launcher,
  whose own child could start outside the job), and it runs a small trusted
  bootstrap that waits for a go signal. That signal is only sent after the child is
  inside the job, so no generated code runs before the caps apply. If the Job
  Object cannot be created or the child cannot be put into it, the sandbox
  **fails closed**: the child is killed before the go signal, none of the
  generated code runs, a warning is logged, and the result is "not verified"
  ("sandbox caps unavailable"). It never falls back to an uncapped run;
- on POSIX, the equivalent memory/CPU/file-size resource limits, set by the
  bootstrap before the go signal. If a required limit cannot be set, the
  generated code does not run and the result is "not verified". macOS often
  refuses the address-space (memory) limit; there it is skipped with a note on
  stderr, so the memory cap is best-effort on macOS only. The process limit
  (`RLIMIT_NPROC`) counts every process of your user (every thread, on Linux),
  not just the run's, so it is set to what you already run plus 128; when that
  count cannot be taken it is left unset and process creation is refused by the
  audit hook alone;
- the bootstrap tells the app it is ready right before it waits for the go
  signal, and the app only sends go after that. If the bootstrap stops before
  that point (limits that could not be set, a malformed config), none of the
  generated code has run and the result is "not verified". Once go is sent,
  every exit code belongs to the generated code, so it cannot pass itself off
  as a sandbox failure;
- the bootstrap's own settings (the throwaway directory and the list of secret
  paths) are passed on stdin, not on the command line, and the generated code's
  stdin holds only the sample input;
- an **audit hook** (`sys.addaudithook`, installed by the bootstrap before any
  generated code runs) that refuses:
  - writing, deleting or renaming files outside the throwaway directory;
  - SQLite altogether (`sqlite3.connect`, `enable_load_extension`,
    `load_extension`): even an in-memory database can `ATTACH` a file anywhere
    on disk, and extensions are native code, so no path check could hold;
  - reading or listing anything outside the throwaway directory and the Python
    installation (its prefixes and `sys.path`), so imports and tracebacks work
    but your other files (another project's `.env`, a browser profile) can't be
    read;
  - opening or listing known secret locations, wherever they are: `~/.claude`
    and `~/.claude.json`, this repo's `.env`, `~/.ssh`, `~/.aws`, git and GitHub CLI credentials, the
    npm, PyPI, Docker, Kubernetes and GnuPG stores (`~/.npmrc`, `~/.pypirc`,
    `~/.docker/config.json`, `~/.kube`, `~/.gnupg`), and the Windows credential
    stores under `%APPDATA%` / `%LOCALAPPDATA%`;
  - all network use: every socket bind, connect and send, and DNS lookups,
    whatever the address (`127.0.0.1` and other local services included). There
    are no exceptions, so `asyncio` is not available in the sandbox: on Windows
    its event loop needs a loopback socket pair, so `asyncio.run(...)` stops the
    run as "blocked by sandbox (socket.bind)". (An earlier exception for that
    socket pair was removed after it was bypassed three times; LeetCode
    solutions need neither sockets nor `asyncio`.)
  - starting processes (`subprocess`, `os.system`, `os.exec*`, `os.spawn*`,
    `multiprocessing`). On POSIX that includes `_posixsubprocess.fork_exec`,
    which raises no audit event of its own: `multiprocessing` calls it directly
    to start spawn-method children, so the bootstrap replaces it with a refusal
    before any generated code runs;
  - loading libraries through `ctypes`, and walking the heap with
    `gc.get_objects` / `get_referrers` / `get_referents`;
  - creating symlinks or junctions, and writing to the registry.

  A run stopped by the hook is reported as "blocked by sandbox (...)" rather than
  a bare exit code (only when the hook's error is the one that ended the run,
  not when the code caught it and failed with something else).

**The audit hook is defence in depth, not a security boundary.** Python's own
documentation says audit hooks cannot sandbox malicious code, and there are known
gaps (for example `dir_fd`-relative opens on POSIX, or native extension modules that
skip the hooks). The hook exists to stop careless or prompt-injected solution code
from reading your Claude login, touching the network, or writing across your disk.
It does not stop code written specifically to get around it.

What the sandbox does **not** do: there is no OS-level filesystem or network
confinement. The code still runs as your user, and anything that gets past the audit
hook can read what you can read. Treat it like running any AI-generated snippet
locally: don't paste a problem whose generated solution you would not be willing to
run yourself.

### The `claude` calls are isolated

`claude -p` is a full Claude Code agent by default. It would load your plugins, hooks,
skills, `CLAUDE.md` and output style, and it could use tools. LeetCoach narrows every
call (`claude_cli.build_argv`):

- `--safe-mode`, `--tools ""` (no built-in tools), `--strict-mcp-config` (no MCP
  servers) and a short `--system-prompt` persona in place of the agent prompt.
  - Each flag is passed only when the installed CLI's cached `claude --help` lists it,
    so an older CLI still works, just with less isolation.
  - If `claude --help` itself fails (times out twice, exits nonzero), the call is
    refused with an error instead of going out without these flags. The probe is
    retried a minute later.
  - `--bare` is never passed, because it drops the subscription login.
- Every call runs in a neutral working directory (`LEETCOACH_CLAUDE_CWD`, by default
  under `%LOCALAPPDATA%\LeetCoach` or `~/.local/share/leetcoach`), never in this repo.
  The CLI cannot pick up the repo's `CLAUDE.md`, settings or `.env` from its cwd. If
  that folder cannot be created, the fallback under the temp folder is per user
  (`leetcoach-claude-cwd-<uid>`); on Linux and macOS it is created `0o700` and one
  owned by another user is refused, so a shared `/tmp` cannot plant a `CLAUDE.md`.
- **Subscription only.** `claude` children never inherit `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX`, from
  the shell or `.env`, so a stray key cannot switch runs to billed API credits. The
  first call logs a warning naming any variable it withheld.
- **Session persistence.** Study runs (`/run`) keep their session, because a later
  follow-up resumes it with `claude -p --resume <session_id>`. That session lives in the
  neutral directory's project bucket, not in your own projects' history. A session id
  read back from the run log must be a plain token (`[A-Za-z0-9_-]`) before it reaches
  argv. Utility calls (the classifier, Quick Ask and the follow-up fallback) pass
  `--no-session-persistence`. The sign-in probe is `claude auth status`, which makes no
  model call.
- The prompt goes on stdin. A watchdog kills the whole `claude` process tree on timeout
  or cancel.

### Keep it local

The app binds to `127.0.0.1`, has no authentication, and intentionally shows the real
error text in the browser to make local debugging easy. Every request's `Host` header
is checked against a loopback allowlist (`127.0.0.1`, `localhost`, `[::1]`) and
anything else gets a 403, so a malicious web page cannot drive the app through your
browser via DNS rebinding.

Other web hardening:

- **CSP.** Every page response carries `default-src 'none'; script-src 'self';
  style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self';
  base-uri 'none'; form-action 'none'; frame-ancestors 'none'`. All scripts, styles and
  fonts are vendored, with no CDN and no inline script.
- **No framing.** `frame-ancestors 'none'` and `X-Frame-Options: DENY` stop
  clickjacking of Run, Delete and the model picker.
- **Plain-text library files.** `X-Content-Type-Options: nosniff` keeps
  `/library/file`, which is served as `text/plain`, from being interpreted as HTML.
- **Same-origin check.** Every unsafe request (POST, PUT, PATCH, DELETE) with an
  `Origin` header must come from this exact origin, and `Sec-Fetch-Site: cross-site` is
  refused. `GET /`, which runs the sign-in probe, may be loaded cross-site only by a
  top-level navigation. JSON routes accept only a JSON object body.
- **Anki export.** In `GET /flashcards.tsv`, any cell a spreadsheet would read as a
  formula (starting with `=`, `+`, `-`, `@`, a tab or a line break) is prefixed with `'`.
  A field holding a tab, a line break or a quote is quoted, so it cannot break the
  columns.

Do not expose LeetCoach to a network or run it as a shared/multi-user service.

## Reporting a vulnerability

If you find a security issue, please report it privately rather than opening a public
issue: use this repository's **Security Advisories** tab on GitHub
("Report a vulnerability"). Please include steps to reproduce. Since this is a personal
project there is no formal SLA, but reports are appreciated and will be looked at.
