"""Manual smoke test for the `claude_cli` keystone — NOT collected by pytest.

Runs a trivial REAL prompt through the actual `claude` CLI and prints the
streamed text deltas, proving the wrapper works end-to-end on this machine.

Run it from the project root with the venv's interpreter::

    py scripts/smoke_claude.py
    # or:  .venv/Scripts/python.exe scripts/smoke_claude.py

Exit code 0 means a real response streamed successfully.
"""
from __future__ import annotations

import os
import sys

# Make the project root importable when run as `scripts/smoke_claude.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claude_cli  # noqa: E402
import config  # noqa: E402


def main() -> int:
    if not claude_cli.is_available():
        print(
            f"FAIL: `{config.claude_bin()}` not found on PATH. Install Claude Code "
            "or set LEETCOACH_CLAUDE_BIN.",
            file=sys.stderr,
        )
        return 1

    prompt = "Reply with exactly: OK"
    print(f"model   : {config.model()}")
    print(f"binary  : {config.claude_bin()}")
    print(f"prompt  : {prompt!r}")
    print("streaming -------------------------------------------------------")

    chunks: list[str] = []
    for delta in claude_cli.run(prompt):
        chunks.append(delta)
        # Show streaming in real time, no extra newline between deltas.
        sys.stdout.write(delta)
        sys.stdout.flush()

    full = "".join(chunks).strip()
    print()
    print("-----------------------------------------------------------------")
    print(f"assembled: {full!r}")
    print(f"deltas   : {len(chunks)}")

    if not full:
        print("FAIL: empty response from claude.", file=sys.stderr)
        return 1
    print("OK: real response streamed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
