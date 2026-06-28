# LeetCoach

A personal, local tool for practicing LeetCode-style problems. Paste a problem, pick a language and a
mode, and LeetCoach drives the **`claude` CLI** to generate study material — then saves it to `output/`
so it grows into your own study library. It's a minimal Flask web app that runs on localhost and
streams Claude's response to the browser live.

## How it works (the `claude` CLI dependency)

LeetCoach does **not** use an API key. It shells out to the `claude` command-line tool (`claude -p`),
which uses your existing **Claude Code subscription**. So before anything works you need:

- The `claude` CLI installed and **on your PATH** (or point `LEETCOACH_CLAUDE_BIN` at its full path).
- That CLI **authenticated** (i.e. `claude` runs and answers from your normal shell).

If `claude` isn't found, the page still loads but shows a banner and runs will fail until it's
installed/authenticated. There are no secrets to configure in this project.

## Setup

Requires **Python 3.12+** (use the `py` launcher on Windows).

```sh
py -m venv .venv
.\.venv\Scripts\Activate.ps1     # PowerShell (use activate / activate.bat / source as appropriate)
pip install -r requirements.txt
```

Optionally copy `.env.example` to `.env` and tweak it (all settings are optional — see
[Configuration](#configuration)).

## Run

```sh
python app.py
```

Then open the printed localhost URL (default `http://127.0.0.1:5000`) in your browser. Paste a
problem, choose a mode + language (+ tier), and click **Run**. The answer streams in live and is saved
under `output/`.

## Modes

Pick one of three modes. Two of them are **tiered** — *simple* (basic, possibly sub-optimal),
*normal* (a balanced interview answer), or *complex* (the most optimal solution).

- **Learning** (no tier) — teaches the full tech stack a problem needs (data structures, algorithms,
  language stdlib tools) so you can solve it yourself. Uses the topic index to skip / cross-link
  topics you've already studied (see add-ons).
- **Guided Learning** (simple / normal / complex) — one flowing document: restate the problem → teach
  the stack → reason step-by-step → produce the answer.
- **Answer** (simple / normal / complex) — a working solution plus step-by-step reasoning, an explicit
  Big-O line, and the trade-off versus the other tiers.

## Add-ons

- **Sample-I/O verification** — Answer/Guided solutions are auto-checked against the problem's own
  `Input:` / `Output:` examples in a sandbox (see the caveat below).
- **Topic index** — Learning records what you've covered (in `topic_index.json`) and feeds it back so
  later runs skip already-learned material and cross-link the prior note.
- **Always-on Big-O** — every Answer/Guided solution must state explicit time and space complexity.

## Verification caveat

Verification is **best-effort and Python-first**:

- **Python** is first-class: the generated script reads the sample input on stdin and prints the
  result; LeetCoach runs it in a throwaway, secret-free sandbox and diffs stdout against the expected
  output → reports `PASS` / `FAIL`.
- **C++ / Java** are **not auto-verified**. LeetCoach only probes for a compiler (`g++` / `javac`); the
  result is shown as "not auto-verified" (auto-run is unsupported) — verify those manually.
- When a problem has no parseable sample I/O, the result is likewise "not auto-verified". A
  not-verified result is never a failure.

## Where outputs are saved

Everything lands under the `output/` directory (gitignored), organised by problem type:

```
output/
  learning/<problem_type>_learning/<problem>.md
  guided/<problem_type>/<problem>.md
  answers/<problem_type>/<problem>__<tier>.<ext>   (code)
  answers/<problem_type>/<problem>__<tier>.md      (reasoning + verification)
  topic_index.json                                 (the topic index)
```

## Configuration

All four settings are environment variables (overridable in your shell or a `.env` file). All are
optional — the defaults work on a machine with `claude` on PATH.

| Variable               | Default                        | What it does                                                        |
| ---------------------- | ------------------------------ | ------------------------------------------------------------------- |
| `LEETCOACH_MODEL`      | `claude-opus-4-8`              | Claude model id passed to `claude --model` (e.g. `opus` / `sonnet`).|
| `LEETCOACH_CLAUDE_BIN` | `claude`                       | Name or absolute path of the `claude` executable.                   |
| `LEETCOACH_OUTPUT_DIR` | `output`                       | Where the study library is written.                                 |
| `LEETCOACH_TOPIC_INDEX`| `<output_dir>/topic_index.json`| Path to the persisted topic index JSON.                             |

## Tests

All tests mock the `claude` subprocess — no real Claude calls are made.

```sh
py -m pytest -q
```
