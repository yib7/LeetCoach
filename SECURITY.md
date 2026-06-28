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
  secret-free environment and a wall-clock timeout.

### The sample-I/O sandbox is a convenience check, not a security boundary

Verification executes model-generated code on your machine. The isolation is
best-effort: a throwaway working directory, an environment scrubbed of your variables,
output caps, and a timeout. On POSIX it also applies memory/CPU resource limits; **on
Windows those limits do not exist**, so the only guards are the timeout and the scrubbed
environment. Treat it like running any AI-generated snippet locally: don't paste a
problem whose generated solution you would not be willing to run yourself.

### Keep it local

The app binds to `127.0.0.1`, has no authentication, and intentionally shows the real
error text in the browser to make local debugging easy. Do not expose it to a network
or run it as a shared/multi-user service.

## Reporting a vulnerability

If you find a security issue, please report it privately rather than opening a public
issue: use this repository's **Security Advisories** tab on GitHub
("Report a vulnerability"). Please include steps to reproduce. Since this is a personal
project there is no formal SLA, but reports are appreciated and will be looked at.
