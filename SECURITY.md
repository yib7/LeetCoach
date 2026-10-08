# Security

## Scope and threat model

LeetCoach is a **single-user tool that runs on `localhost`** and drives your own
authenticated `claude` CLI. It has no accounts, no login, and stores nothing but the
study notes you generate. It holds no API keys or secrets: it uses your Claude Code
subscription through the CLI, not an API key.

Two parts handle untrusted input, and both are deliberately contained:

- **Pasted problem text** only ever flows into the prompt (sent to `claude` on stdin,
  never on the command line) and into filename generation, where a single `slug()`
  function strips path separators and `..` so a write can never escape `output/`.
- **Answer / Guided verification** runs the Python solution Claude generated, to check
  it against the problem's own sample I/O. This runs in a throwaway directory with a
  secret-free environment, resource caps, and a wall-clock timeout.

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
- on POSIX, the equivalent memory/CPU/file-size/process resource limits, set by
  the bootstrap before the go signal. If a required limit cannot be set, the
  generated code does not run and the result is "not verified";
- the bootstrap's own settings (the throwaway directory and the list of secret
  paths) are passed on stdin, not on the command line, and the generated code's
  stdin holds only the sample input;
- an **audit hook** (`sys.addaudithook`, installed by the bootstrap before any
  generated code runs) that refuses:
  - writing, deleting or renaming files outside the throwaway directory, including
    SQLite database files (in-memory databases are fine);
  - opening or listing known secret locations: `~/.claude` and `~/.claude.json`,
    this repo's `.env`, `~/.ssh`, `~/.aws`, git and GitHub CLI credentials, and
    the Windows credential stores under `%APPDATA%` / `%LOCALAPPDATA%`;
  - network connections and DNS lookups. The single exception is a socket pair
    inside the process itself (bind to a free port on `127.0.0.1`, then connect to
    that same listener), which `asyncio` needs on Windows; connecting to any other
    address, local services included, is still refused;
  - starting processes (`subprocess`, `os.system`, `os.exec*`, `os.spawn*`,
    `multiprocessing`);
  - loading libraries through `ctypes`, and walking the heap with
    `gc.get_objects` / `get_referrers` / `get_referents`;
  - creating symlinks or junctions, and writing to the registry.

  A run stopped by the hook is reported as "blocked by sandbox (...)" rather than
  a bare exit code.

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

### Keep it local

The app binds to `127.0.0.1`, has no authentication, and intentionally shows the real
error text in the browser to make local debugging easy. Every request's `Host` header
is checked against a loopback allowlist (`127.0.0.1`, `localhost`, `[::1]`) and
anything else gets a 403, so a malicious web page cannot drive the app through your
browser via DNS rebinding. Do not expose it to a network or run it as a
shared/multi-user service.

## Reporting a vulnerability

If you find a security issue, please report it privately rather than opening a public
issue: use this repository's **Security Advisories** tab on GitHub
("Report a vulnerability"). Please include steps to reproduce. Since this is a personal
project there is no formal SLA, but reports are appreciated and will be looked at.
