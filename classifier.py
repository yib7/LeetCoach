"""Classify a pasted problem into a `problem_type` slug + a topic list.

One short Claude call (through the injectable ``run_fn``, defaulting to
``claude_cli.run``) asks for a tiny JSON object. The parser is deliberately
forgiving: Claude often wraps JSON in prose or ```` ```json ```` fences, so we
extract the first balanced ``{...}`` object and parse that. Anything we cannot
make sense of degrades to a safe fallback (``uncategorized`` / no topics) rather
than raising — classification is best-effort metadata, never a hard dependency.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import claude_cli
import storage

FALLBACK_TYPE = "uncategorized"

# The prompt asks for exactly this shape so parsing stays trivial in the common
# case. We still tolerate prose/fences around it (see _extract_json).
_CLASSIFY_INSTRUCTIONS = (
    "Classify the following LeetCode-style problem. Respond with ONLY a tiny "
    "JSON object, no prose, of the form:\n"
    '{"problem_type": "<snake_case_category>", "topics": ["topic1", "topic2"]}\n'
    "where problem_type is a short snake_case slug for the dominant technique "
    "(e.g. two_pointers, sliding_window, dynamic_programming, bfs, backtracking) "
    "and topics lists the data structures / algorithms the problem touches.\n\n"
    "--- BEGIN PROBLEM ---\n"
    "__PROBLEM__\n"
    "--- END PROBLEM ---"
)


@dataclass
class Classification:
    """Result of classifying a problem."""

    problem_type: str
    topics: list[str] = field(default_factory=list)


def build_classify_prompt(problem: str) -> str:
    """Return the prompt sent to Claude for classification."""
    # Simple substitution (not str.format) because the template intentionally
    # contains literal JSON braces that would confuse format().
    return _CLASSIFY_INSTRUCTIONS.replace("__PROBLEM__", problem)


def _extract_json(text: str) -> dict | None:
    """Best-effort: pull the first balanced JSON object out of ``text``.

    Handles bare JSON, fenced ```` ```json ... ``` ```` blocks, and JSON
    embedded in surrounding prose. Returns the parsed dict, or ``None`` if no
    valid object is found.
    """
    if not text:
        return None

    # Fast path: the whole thing is JSON.
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Scan for balanced {...} spans and try each, longest-first is unnecessary —
    # the first valid object wins.
    start = None
    depth = 0
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        start = None
                        continue
                    if isinstance(obj, dict):
                        return obj
                    start = None
    return None


def _coerce_topics(raw) -> list[str]:
    """Normalise the ``topics`` field into a clean list of non-empty strings."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            t = item.strip()
            if t:
                out.append(t)
    return out


def classify(problem: str, *, run_fn=claude_cli.run, **run_kwargs) -> Classification:
    """Classify ``problem`` into a ``Classification``.

    Parameters
    ----------
    problem:
        The pasted problem text.
    run_fn:
        Injectable Claude runner with the ``claude_cli.run`` signature
        (``run_fn(prompt, **kwargs) -> Iterable[str]`` of text deltas). Tests
        pass a fake so no real Claude is spawned.
    **run_kwargs:
        Forwarded to ``run_fn`` (e.g. ``model=``).

    Never raises on bad output: unparseable replies fall back to
    ``Classification(problem_type="uncategorized", topics=[])``.
    """
    prompt = build_classify_prompt(problem)
    try:
        text = "".join(run_fn(prompt, **run_kwargs))
    except Exception:
        # A flaky/missing Claude must not crash the caller; classification is
        # best-effort metadata.
        return Classification(FALLBACK_TYPE, [])

    obj = _extract_json(text)
    if obj is None:
        return Classification(FALLBACK_TYPE, [])

    topics = _coerce_topics(obj.get("topics"))

    raw_type = obj.get("problem_type")
    if isinstance(raw_type, str) and raw_type.strip():
        problem_type = storage.slug(raw_type)
        # slug() never returns ""; all-garbage/punctuation labels collapse to its
        # "untitled" sentinel. A legitimate problem_type is always a snake_case
        # technique slug, never "untitled", so treat that sentinel as a miss and
        # route it to the real fallback bucket rather than a stray "untitled" one.
        if problem_type == "untitled":
            problem_type = FALLBACK_TYPE
    else:
        problem_type = FALLBACK_TYPE

    return Classification(problem_type, topics)
