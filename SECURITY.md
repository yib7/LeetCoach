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
  survives the run;
- on POSIX, the equivalent memory/CPU/file-size resource limits.

What it does **not** do: there is no filesystem confinement (beyond the scrubbed
environment, the code runs as your user and can read or write anything you can)
and no network confinement, so it can open sockets. Treat it like running any
AI-generated snippet locally: don't paste a problem whose generated solution you
would not be willing to run yourself.

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
