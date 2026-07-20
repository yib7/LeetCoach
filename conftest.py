"""Make the project root importable so tests can `import claude_cli`, `config`, etc.

Placing conftest.py at the repo root puts that directory on sys.path for the
whole pytest session, independent of how/where pytest is invoked. The ``tests``
directory is added too, so test modules can ``from _helpers import ...`` (the
shared SSE-parser / classifier-fixture helpers) regardless of pytest's import
mode.
"""
import os
import sys

_ROOT = os.path.dirname(__file__)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
