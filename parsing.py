"""Pull the primary code block out of a Claude markdown answer.

The web layer (and SP5's sandbox) needs the *runnable* code separated from the
surrounding prose so it can be saved to a ``.py`` / ``.cpp`` / ``.java`` file and
later fed to a verifier, while the prose becomes the sibling reasoning ``.md``.

``extract_code`` is deliberately small and dependency-free so the sandbox can
import it without dragging in Flask. It is forgiving: if Claude forgets to fence
its code (or fences it without a language tag), it still does something sensible
rather than raising.
"""
from __future__ import annotations

import re

# A fenced block: ```lang\n ... \n``` . The language tag is optional and the
# closing fence may sit at end-of-string without a trailing newline.
_FENCE = re.compile(
    r"```[ \t]*([A-Za-z0-9_+#-]*)[ \t]*\r?\n(.*?)```",
    re.DOTALL,
)

# Map our language keys (and common aliases Claude might emit) to a canonical
# fence-tag set, so ```python and ```py both match the python request.
_LANG_ALIASES = {
    "python": {"python", "py", "python3"},
    "cpp": {"cpp", "c++", "cxx", "cc", "c"},
    "java": {"java"},
}


def extract_code(markdown: str, language: str) -> str:
    """Return the primary fenced code block from ``markdown``.

    Selection order:

    1. The first fenced block whose language tag matches ``language`` (honouring
       aliases like ``py`` for ``python``).
    2. Otherwise the first fenced block of any language (Claude sometimes omits
       or mis-tags the tag).
    3. Otherwise the empty string — the caller treats the whole document as
       reasoning when there is no extractable code.

    The returned code is stripped of a single trailing newline only; internal
    formatting is preserved verbatim so it stays runnable.
    """
    if not markdown:
        return ""

    blocks = _FENCE.findall(markdown)  # list[(tag, body)]
    if not blocks:
        return ""

    wanted = _LANG_ALIASES.get((language or "").lower(), set())

    # Pass 1: a block explicitly tagged with the requested language.
    if wanted:
        for tag, body in blocks:
            if tag.lower() in wanted:
                return body.rstrip("\n")

    # Pass 2: the first fenced block of any kind.
    return blocks[0][1].rstrip("\n")
