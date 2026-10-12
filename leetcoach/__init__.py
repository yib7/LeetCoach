"""LeetCoach: a local Flask app that turns a pasted LeetCode problem into study
material by driving the ``claude`` CLI. Start it with ``python app.py`` from the
repo root; ``docs/ARCHITECTURE.md`` maps the modules."""
import sys

# The README's floor. Checked here, before any submodule loads, so an older
# interpreter stops with one clear line instead of failing later inside a run.
# Keep this file to syntax any Python 3.6+ can parse.
MIN_PYTHON = (3, 12)


def python_too_old(version_info=None):
    """The message to stop with when ``version_info`` is below
    :data:`MIN_PYTHON`, else ``None``."""
    version = tuple(sys.version_info if version_info is None else version_info)
    if version[:2] >= MIN_PYTHON:
        return None
    return (
        f"LeetCoach needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is "
        f"Python {version[0]}.{version[1]}. Install a newer Python and set up the "
        ".venv again (see the README)."
    )


_too_old = python_too_old()
if _too_old:
    raise SystemExit(_too_old)
