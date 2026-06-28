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

# Windows reserved device names — illegal as a filename even with an extension,
# so a slug must never emit one bare (the project's primary platform is Windows).
_WIN_RESERVED = (
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

# Cap a single path segment so a long input (e.g. a whole pasted problem) can't
# build a filename that blows past the OS path limit — Windows MAX_PATH is ~260.
_MAX_SLUG = 80


def slug(name: str) -> str:
    """Return a filesystem-safe, lowercase slug derived from ``name``.

    Guarantees (see tests):

    * lowercase; spaces and other punctuation collapse to single ``_``;
    * ``-`` and ``_`` are preserved (they are valid, readable separators);
    * path separators (``/`` ``\\``) and ``..`` are stripped — a slug can never
      contain a directory boundary or a traversal token;
    * leading/trailing separators are trimmed;
    * length is capped at ``_MAX_SLUG`` so a huge input can't overflow the OS
      path limit;
    * Windows reserved device names (``con`` / ``nul`` / ``com1`` ...) are
      suffixed so the slug is always a legal filename on Windows;
    * never empty — all-garbage input yields the literal ``"untitled"`` so a
      filename always exists.
    """
    s = (name or "").strip().lower()
    s = _UNSAFE.sub("_", s)
    # Collapse any accidental runs and trim separator chars off the ends so we
    # don't get names like ``_foo_`` or ``--bar``.
    s = re.sub(r"_+", "_", s)
    s = s.strip("_-") or "untitled"
    if len(s) > _MAX_SLUG:
        s = s[:_MAX_SLUG].rstrip("_-") or "untitled"
    # A bare Windows device name can't be a filename even with an extension;
    # suffix it so a write never fails on the project's primary platform.
    if s in _WIN_RESERVED:
        s = f"{s}_"
    return s


def _problem_name(problem: str) -> str:
    """A short, clean filename stem for a pasted problem.

    Uses the first non-blank line — the title, for a standard LeetCode paste —
    so a full multi-line paste saves as e.g. ``two_sum.md`` instead of a name
    built from the entire description. ``slug`` caps the length either way, which
    is what stops a giant paste from overflowing the OS path limit.
    """
    for line in (problem or "").splitlines():
        if line.strip():
            return slug(line)
    return slug(problem)


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
    path = root / "learning" / folder / f"{_problem_name(problem)}.md"
    return _write(path, body)


def save_guided(problem: str, problem_type: str, body: str) -> str:
    """Write a Guided-Learning doc and return its path.

    -> ``output/guided/<problem_type>/<problem>.md``
    """
    root = config.output_dir()
    path = root / "guided" / slug(problem_type) / f"{_problem_name(problem)}.md"
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
    stem = f"{_problem_name(problem)}__{slug(tier)}"
    code_path = folder / f"{stem}.{ext}"
    reasoning_path = folder / f"{stem}.md"
    return _write(code_path, code), _write(reasoning_path, reasoning)
