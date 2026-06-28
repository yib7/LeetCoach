"""Make the project root importable so tests can `import claude_cli`, `config`, etc.

Placing conftest.py at the repo root puts that directory on sys.path for the
whole pytest session, independent of how/where pytest is invoked.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
