# Daily-use QOL, round 2 — design

**Date:** 2026-09-08
**Branch:** `feature/daily-use-qol` (continues the v1.4.0 QOL work; still unmerged)
**Goal:** Three user-requested quality-of-life changes — rename the tier to "Code
Quality" with friendlier labels, let the user switch the Run model from inside the app,
and make `claude` sign-in friction-free from the desktop shortcut.

## Context

The user redirected mid-cycle with three concrete asks (a user instruction, so it
outranks the frozen scope) and answered the branching questions:

1. **Tier rename** — rename to "Code Quality": Basic / Normal / Optimal, **and rename the
   saved answer files too** (migrate the existing library).
2. **Model switch** — an in-app picker offering the **generic aliases** Opus / Sonnet /
   Haiku, and the choice **persists to `.env`** as the default.
3. **Auth** — the shortcut should start `claude` sign-in when needed; if that isn't
   possible, the in-app error must give the exact copy-paste command.

Live probe of the installed CLI settled the feasibility questions:

- `claude auth status` → JSON `{"loggedIn": <bool>, "authMethod": ..., ...}` — instant,
  offline, non-interactive. A clean auth probe.
- `claude auth login` → "Sign in to your Anthropic account" — the exact command.
- The user's current failure is `loggedIn: false` (expired/absent OAuth), which is why
  runs exit 1.

## Feature 1 — Tier → "Code Quality" (rename values, migrate files)

**Values.** `prompts.TIERS` becomes `("basic", "normal", "optimal")`. `_TIER_DESC` re-keys
to those and its wording updates (`basic` = simplest/possibly sub-optimal, `normal` =
balanced, `optimal` = best time/space). `_answer_fragment` interpolates the new key
(`at the **optimal** tier`). `_check_tier` and the `TIERS` re-export in `app.py` follow
automatically.

**UI.** `templates/index.html` control: label `Tier` → `Code Quality`; buttons
`Basic` (val `basic`) / `Normal` (val `normal`, default `on`) / `Optimal` (val `optimal`).

**Filenames + migration.** New answers save as `<problem>__basic.*` / `__normal.*` /
`__optimal.*` (unchanged mechanism; only the tier token changes). A one-time idempotent
`storage.migrate_tier_suffixes(root)` renames existing `__simple`→`__basic` and
`__complex`→`__optimal` under `output/answers/`. It is safe to split the stem on `__`
because `slug()` collapses `_+`→ a single `_`, so `__` only ever delimits the tier (and an
optional `__<n>` slot). The tier is always the second `__`-segment. Migration skips a
rename whose destination already exists (idempotent, never clobbers). Called once from the
Flask app factory / startup, guarded so a failure only logs (never blocks the app).

## Feature 2 — In-app model switch (aliases, persisted to `.env`)

**UI.** A segmented control `data-seg="model"` in the Console controls row, always visible:
`Opus` (val `opus`) / `Sonnet` (val `sonnet`) / `Haiku` (val `haiku`). Initial active
button derives from the current `config.model()` by substring (`claude-opus-4-8` → Opus).

**Endpoint.** `POST /config/model` with `{"model": "<alias>"}`:
- validate against the allowlist `{"opus", "sonnet", "haiku"}` (reject others 400 — the
  value becomes a `--model` argv token, so it is allowlisted, not free-form);
- upsert `LEETCOACH_MODEL=<alias>` into `.env` next to `app.py`, preserving all other
  lines and creating the file if absent;
- set `os.environ["LEETCOACH_MODEL"]` in-process so it takes effect on the next run with
  no restart (`config.model()` reads env at call time);
- return `{"ok": true, "model": <alias>}`.

**Run path.** Unchanged: `/run` uses `config.model()`, which now reflects the picked alias.
Quick Ask and the classifier keep their own Haiku defaults.

**`.env` upsert helper.** A small pure function `upsert_env_var(path, key, value)` (line-
oriented: replace an existing `KEY=` line in place, else append) so it is unit-testable
without touching the real `.env`.

## Feature 3 — Friction-free `claude` auth

**Probe.** `claude_cli.auth_status(*, runner=...)` runs `claude auth status`, parses the
JSON, and returns a small dataclass/dict `{installed: bool, logged_in: bool}`. Runner is
injectable so tests never call the real CLI. `installed=False` when the binary is missing
(FileNotFoundError) or the probe errors; `logged_in` mirrors the JSON `loggedIn`.

**Launcher.** New `scripts/ensure-claude-auth.ps1`:
- run `claude auth status`; if the binary is missing, print an install pointer and exit 0
  (let the app load and show its banner);
- if `loggedIn` is false, print a friendly line and run `claude auth login` (interactive
  OAuth in the console), then re-check;
- if still not logged in, print the exact copy-paste command (`claude auth login`) and
  continue anyway — never hard-block the app from starting.
`LeetCoach.cmd` calls this script (after the venv bootstrap, before `python app.py`).
`python app.py`'s `__main__` prints the same signed-out guidance (it does not launch the
interactive login — that is the launcher's job) so a terminal user is guided too.

**Banner + error copy.** The index route passes both an `installed` and a `logged_in`
flag to the page. The banner text distinguishes:
- not installed → install/PATH guidance (existing);
- installed but signed out → "You're signed out of the `claude` CLI. Run `claude auth
  login` in a terminal, then reload." with the command shown for copy-paste.
The run-time error in `claude_cli` (already surfacing claude's real stdout message) gains
the same `claude auth login` copy-paste line.

## Testing

- **prompts:** `TIERS == ("basic","normal","optimal")`; `build_answer(tier="optimal")`
  contains "optimal" and the optimal description; invalid old value `"complex"` now raises.
- **storage:** `migrate_tier_suffixes` renames `__simple`/`__complex` pairs (code + .md),
  leaves `__normal` and unrelated files alone, is idempotent, and does not clobber an
  existing destination. Slug-underscore edge case (`two_sum__simple`) migrates correctly.
- **config/env:** `upsert_env_var` replaces an existing line, appends a missing one,
  creates the file, and leaves other lines untouched.
- **/config/model:** valid alias writes `.env` + sets `os.environ` + 200; unknown alias
  → 400 and no write; `config.model()` reflects the change.
- **auth_status:** logged-in JSON → `logged_in True`; logged-out JSON → False; missing
  binary → `installed False`; malformed output → safe default (installed True/False per
  runner, logged_in False), never raises.
- **index route:** signed-out probe injects a `logged_in=false` flag into the page.

All tests mock the subprocess / injected runners; the suite stays offline. Update the test
count in README/CHANGELOG once the final number is known.

## Out of scope / notes

- No per-run transient model override — the picker sets the persistent default (user's
  choice). A future backlog item could add a "just this run" toggle.
- Aliases only in the picker; explicit `LEETCOACH_MODEL=claude-opus-5` remains possible by
  editing `.env` by hand.
- Merging to `main` stays the human gate.
