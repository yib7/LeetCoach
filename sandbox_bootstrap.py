"""Trusted bootstrap for the Answer-mode sandbox (SP3 A5). STDLIB ONLY.

``sandbox.verify_python`` spawns ``<base python> -I -X utf8 -B sandbox_bootstrap.py
<solution.py> <run_dir> <config-json>`` instead of running the untrusted
solution directly, and this file is what runs first in the child:

1. **Handshake (A5).** Block on an unbuffered ONE-byte ``os.read(0, 1)`` until
   the parent sends the go byte. The parent sends it only AFTER it has put this
   process into the Windows Job Object, so no untrusted line can allocate or
   spawn outside the memory / process caps, however long a GIL-starved parent
   takes to get to ``AssignProcessToJobObject``. The raw 1-byte read leaves
   the rest of stdin (the sample input) untouched in the pipe, so every way a
   solution reads stdin — ``input()``, ``sys.stdin``, ``sys.stdin.buffer``,
   ``open(0)``, ``os.read(0, n)`` — sees exactly the sample and nothing else.
2. Make the solution feel like ``python solution.py``: ``__name__ ==
   "__main__"``, ``sys.argv == [solution.py]``, the run dir first on
   ``sys.path``, then ``runpy.run_path``. Exit codes and tracebacks propagate
   unchanged (an uncaught exception still exits 1 with a traceback).

It must stay tiny and trusted: it runs under ``-I`` (no user site, no
``PYTHON*`` env vars, script dir not on ``sys.path``), so it imports nothing
from the repo.
"""
import json
import os
import runpy
import sys
import traceback

GO = b"\x01"
EXIT_NO_GO = 97  # parent vanished / closed stdin before releasing us


def main(argv: list) -> None:
    script, run_dir, raw_cfg = argv[1], argv[2], argv[3]
    json.loads(raw_cfg)  # validated now, used by later hardening stages

    # (1) The handshake. Nothing below this line runs until the parent has
    # assigned the job object.
    if os.read(0, 1) != GO:
        os._exit(EXIT_NO_GO)

    # (2) Run the untrusted solution as if it were the main script.
    sys.argv[:] = [script]
    sys.path.insert(0, run_dir)
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - mirror the interpreter's own handler
        # Same exit code (1) and traceback as `python solution.py`, minus the
        # bootstrap/runpy frames, so the user's stderr shows only their code.
        tb = exc.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_filename != script:
            tb = tb.tb_next
        traceback.print_exception(type(exc), exc, tb)
        sys.stderr.flush()
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
