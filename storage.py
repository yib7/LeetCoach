"""Persist study outputs into the `output/` library, with safe filenames.

The web layer calls a handful of small functions here; each writes a file and
returns the path it wrote, so the caller can show / link it.

Directory layout (from the plan):

    output/
      learning/<problem_type>_learning/<problem>.md
      guided/<problem_type>/<problem>.md
      answers/<problem_type>/<problem>__<tier>.<ext>   (+ a sibling .md)

The single most important property is **containment**: a hostile problem name
like ``../../etc/passwd`` (or an absolute path, or one full of backslashes) must
never let a write escape ``config.output_dir()``. We achieve that by running
every user-supplied path segment through :func:`slug`, which strips path
separators and ``..`` entirely before they can be interpreted as a directory.
``slug`` is the only thing standing between user input and the filesystem, so it
is deliberately strict and well-tested.
"""
from __future__ import annotations

import re
from pathlib import Path

import config

# Extension chosen per answer language. Anything unknown falls back to ``.txt``
# so an unexpected language never produces a separator-bearing extension.
_LANG_EXT = {
    "python": "py",
    "cpp": "cpp",
    "java": "java",
}

# Any run of characters that is NOT a lowercase letter, digit, hyphen or
# underscore becomes a single underscore. Crucially this maps ``/``, ``\`` and
# ``.`` (so ``..``) to underscores, which is what guarantees containment.
_UNSAFE = re.compile(r"[^a-z0-9_-]+")


def slug(name: str) -> str:
    """Return a filesystem-safe, lowercase slug derived from ``name``.

    Guarantees (see tests):

    * lowercase; spaces and other punctuation collapse to single ``_``;
    * ``-`` and ``_`` are preserved (they are valid, readable separators);
    * path separators (``/`` ``\\``) and ``..`` are stripped — a slug can never
      contain a directory boundary or a traversal token;
    * leading/trailing separators are trimmed;
    * never empty — all-garbage input yields the literal ``"untitled"`` so a
      filename always exists.
    """
    s = (name or "").strip().lower()
    s = _UNSAFE.sub("_", s)
    # Collapse any accidental runs and trim separator chars off the ends so we
    # don't get names like ``_foo_`` or ``--bar``.
    s = re.sub(r"_+", "_", s)
    s = s.strip("_-")
    return s or "untitled"


def _ensure_dir(path: Path) -> Path:
    """Create ``path`` (a directory) and its parents; return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path, body: str) -> str:
    """Write ``body`` to ``path`` (UTF-8), creating parents; return str path."""
    _ensure_dir(path.parent)
    path.write_text(body, encoding="utf-8")
    return str(path)


def save_learning(problem: str, problem_type: str, body: str) -> str:
    """Write a Learning note and return its path.

    -> ``output/learning/<problem_type>_learning/<problem>.md``
    """
    root = config.output_dir()
    folder = f"{slug(problem_type)}_learning"
    path = root / "learning" / folder / f"{slug(problem)}.md"
    return _write(path, body)


def save_guided(problem: str, problem_type: str, body: str) -> str:
    """Write a Guided-Learning doc and return its path.

    -> ``output/guided/<problem_type>/<problem>.md``
    """
    root = config.output_dir()
    path = root / "guided" / slug(problem_type) / f"{slug(problem)}.md"
    return _write(path, body)


def save_answer(
    problem: str,
    problem_type: str,
    *,
    tier: str,
    language: str,
    code: str,
    reasoning: str,
) -> tuple[str, str]:
    """Write an Answer's code file plus a sibling reasoning markdown.

    -> code:      ``output/answers/<problem_type>/<problem>__<tier>.<ext>``
    -> reasoning: ``output/answers/<problem_type>/<problem>__<tier>.md``

    Returns ``(code_path, reasoning_path)``. The extension is chosen from
    ``language`` (``py`` / ``cpp`` / ``java``), defaulting to ``txt``.
    """
    root = config.output_dir()
    ext = _LANG_EXT.get(slug(language), "txt")
    folder = root / "answers" / slug(problem_type)
    stem = f"{slug(problem)}__{slug(tier)}"
    code_path = folder / f"{stem}.{ext}"
    reasoning_path = folder / f"{stem}.md"
    return _write(code_path, code), _write(reasoning_path, reasoning)
