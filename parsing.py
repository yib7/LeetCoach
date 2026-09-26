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

# A fenced block: ```lang\n ... \n``` (optionally followed by a trailing word
# like ``solution``, e.g. the doc contract's ```python solution```). The
# language tag is optional and the closing fence may sit at end-of-string
# without a trailing newline.
#
# Both the opening and closing fence are anchored to the START OF A LINE
# (``^`` under MULTILINE), and the closing fence must repeat the SAME leading
# indentation as the opening one (the ``\1`` backreference to the captured
# indent). This is what makes the match indentation-aware in both directions:
#
# * a top-level fence (indent = "") only closes on a column-0 ``` — a nested
#   triple-backtick *inside* the code (e.g. a fenced example in a docstring)
#   is indented, so it's skipped and the non-greedy body runs on to the true
#   closing fence (P2-5) instead of truncating early;
# * a fence NESTED inside a markdown list item (indent = "   ") closes on the
#   next ``` at that SAME indentation, so indented list fences are matched at
#   all (A2) rather than either mis-closing early or never closing.
#
# Residual known limitation: a nested fence at exactly the SAME indentation as
# its enclosing fence would still close it early — vanishingly rare in
# practice (nested examples are conventionally indented one level deeper).
_FENCE = re.compile(
    r"^([ \t]*)```[ \t]*([A-Za-z0-9_+#-]*)[ \t]*([^\n]*)\r?\n(.*?)^\1```",
    re.DOTALL | re.MULTILINE,
)

# Map our language keys (and common aliases Claude might emit) to a canonical
# fence-tag set, so ```python and ```py both match the python request.
_LANG_ALIASES = {
    "python": {"python", "py", "python3"},
    "cpp": {"cpp", "c++", "cxx", "cc", "c"},
    "java": {"java"},
}

# Substrings/patterns that mark a block as the REAL runnable answer rather
# than an earlier teaching/illustrative snippet (A2) — a `__main__` driver, a
# LeetCode-style `class Solution`, or any function definition.
_MAIN_MARKER = "__main__"
_CLASS_SOLUTION_MARKER = "class solution"
_DEF_RE = re.compile(r"(?m)^\s*def\s")


def _has_solution_marker(body: str) -> bool:
    lowered = body.lower()
    if _MAIN_MARKER in lowered or _CLASS_SOLUTION_MARKER in lowered:
        return True
    return _DEF_RE.search(body) is not None


def _dedent(indent: str, body: str) -> str:
    """Strip a fence's own leading ``indent`` from each line of its body.

    Only the fence's list/quote indentation is removed — the code's OWN
    internal indentation (e.g. a function body) is preserved relative to its
    first line, so the extracted code stays syntactically valid.
    """
    if not indent:
        return body
    out = []
    for line in body.split("\n"):
        if line.startswith(indent):
            out.append(line[len(indent):])
        elif not line.strip():
            out.append("")
        else:
            out.append(line)  # under-indented line: leave as-is (best effort)
    return "\n".join(out)


def extract_code(markdown: str, language: str) -> str:
    """Return the primary fenced code block from ``markdown``.

    Selection order (A2 — Guided/Answer docs often lead with a teaching
    snippet before the real answer, so "first" is the wrong default):

    1. The (first) block tagged with the requested language AND the word
       ``solution`` in its fence line (the doc contract's
       ```` ```<lang> solution```` ), e.g. ```python solution```.
    2. Otherwise the LAST language-tagged block that looks like a real answer
       (contains ``__main__``, ``class Solution``, or a ``def``).
    3. Otherwise the LAST block tagged with the requested language.
    4. Otherwise the first fenced block of any language (Claude sometimes
       omits or mis-tags the tag).
    5. Otherwise the empty string — the caller treats the whole document as
       reasoning when there is no extractable code.

    The returned code is stripped of a single trailing newline only; internal
    formatting is preserved verbatim (fence-level indentation aside — see
    :func:`_dedent`) so it stays runnable.
    """
    if not markdown:
        return ""

    raw_blocks = _FENCE.findall(markdown)  # [(indent, tag, trailing, body), ...]
    if not raw_blocks:
        return ""

    # Normalize once: dedent + strip the single trailing newline every block
    # keeps, so every selection tier below just picks a (tag, trailing, body).
    blocks = [
        (tag, trailing, _dedent(indent, body).rstrip("\n"))
        for indent, tag, trailing, body in raw_blocks
    ]

    wanted = _LANG_ALIASES.get((language or "").lower(), set())

    if wanted:
        lang_blocks = [b for b in blocks if b[0].lower() in wanted]
        if lang_blocks:
            # 1) explicit ```<lang> solution``` tag wins outright.
            for tag, trailing, body in lang_blocks:
                if "solution" in trailing.lower():
                    return body
            # 2) the LAST block that looks like a real runnable answer.
            marker_blocks = [b for b in lang_blocks if _has_solution_marker(b[2])]
            if marker_blocks:
                return marker_blocks[-1][2]
            # 3) no signal either way: the LAST language-tagged block (Guided
            #    pipes restate -> teach -> reason -> answer, so the real
            #    solution is the one furthest down, not the first).
            return lang_blocks[-1][2]

    # 4) no block matched the requested language at all: first fence of any kind.
    return blocks[0][2]
