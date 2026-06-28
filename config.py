"""Central config for LeetCoach. Every value is environment-overridable.

These three knobs are the only machine-specific settings the app needs:

- ``LEETCOACH_MODEL``      — Claude model id passed to ``claude --model`` (default
                             ``claude-opus-4-8``; override with a smaller/faster
                             alias like ``sonnet`` to save your subscription budget).
- ``LEETCOACH_CLAUDE_BIN`` — name/path of the ``claude`` executable (default
                             ``claude``; set an absolute path if it is not on PATH).
- ``LEETCOACH_OUTPUT_DIR`` — where the study library is written (default ``output``).

Reading env at *call time* (not import time) keeps tests able to monkeypatch the
environment without re-importing the module.
"""
from __future__ import annotations

import os
from pathlib import Path

# Defaults live here so they are documented in one place and referenced by name.
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_OUTPUT_DIR = "output"


def model() -> str:
    """Claude model id used for every `claude --model <id>` call."""
    return os.environ.get("LEETCOACH_MODEL", DEFAULT_MODEL)


def claude_bin() -> str:
    """Name or absolute path of the `claude` executable."""
    return os.environ.get("LEETCOACH_CLAUDE_BIN", DEFAULT_CLAUDE_BIN)


def output_dir() -> Path:
    """Root directory of the generated study library (created lazily elsewhere)."""
    return Path(os.environ.get("LEETCOACH_OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
