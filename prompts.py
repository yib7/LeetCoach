"""Prompt construction for the three study modes.

Public builders
---------------
* :func:`build_learning` — no tier; teaches the data structures, algorithms and
  language stdlib needed for the problem (optionally skipping topics already
  learned).
* :func:`build_answer` — tiered (simple / normal / complex); produces code plus
  step-by-step reasoning and an explicit time/space Big-O line, calling out the
  trade-off vs the other tiers.
* :func:`build_guided` — tiered; one piped document that restates the problem,
  teaches the stack (Learning fragment), reasons through it, then answers
  (Answer fragment). Reuses the same fragments so the modes stay consistent.

Design: the modes share small reusable *fragments* so wording can't drift
between them — notably the language stdlib hint, the Big-O instruction, and the
tier description. Guided literally composes the Learning-teach and Answer-reason
fragments, which is why all three feel like one coherent voice.
"""
from __future__ import annotations

# --- supported values ----------------------------------------------------

LANGUAGES = ("python", "cpp", "java")
TIERS = ("simple", "normal", "complex")

# Human-facing display names for each language.
_LANG_NAME = {
    "python": "Python",
    "cpp": "C++",
    "java": "Java",
}

# A short, concrete stdlib/tooling hint per language. The teach + answer prompts
# both interpolate this so Claude reaches for idiomatic built-ins. Tests assert
# the leading token of each list is present (heapq / priority_queue / PriorityQueue).
_LANG_STDLIB = {
    "python": (
        "Python's standard library: heapq, collections.deque, collections.Counter, "
        "collections.defaultdict, bisect, itertools, functools.lru_cache"
    ),
    "cpp": (
        "C++ STL: priority_queue, std::vector, std::unordered_map, std::set, "
        "std::deque, std::sort, std::lower_bound"
    ),
    "java": (
        "Java standard library: PriorityQueue, ArrayDeque, HashMap, TreeMap, "
        "Collections.sort, Arrays.binarySearch"
    ),
}

# Per-tier semantics. simple = basic / maybe sub-optimal; normal = balanced;
# complex = best time/space, minimal-but-readable.
_TIER_DESC = {
    "simple": (
        "the SIMPLEST, most basic approach that a beginner could write. Use little "
        "or no library magic. It is acceptable if this is sub-optimal in time or "
        "space complexity — clarity beats cleverness here."
    ),
    "normal": (
        "a realistic, balanced solution — the kind you would strive for in a normal "
        "interview. Balance readability against efficiency without over-engineering."
    ),
    "complex": (
        "the most optimal solution achievable, with the best possible time and space "
        "complexity. Keep it minimal but readable — nothing redundant, no clever code "
        "that hurts clarity."
    ),
}


def _check_language(language: str) -> str:
    if language not in LANGUAGES:
        raise ValueError(
            f"unsupported language {language!r}; expected one of {LANGUAGES}"
        )
    return language


def _check_tier(tier: str) -> str:
    if tier not in TIERS:
        raise ValueError(f"unsupported tier {tier!r}; expected one of {TIERS}")
    return tier


# --- reusable fragments --------------------------------------------------

def _problem_block(problem: str) -> str:
    return (
        "Here is the LeetCode-style problem (verbatim):\n"
        "--- BEGIN PROBLEM ---\n"
        f"{problem}\n"
        "--- END PROBLEM ---"
    )


def _teach_fragment(language: str, already_learned_topics=None) -> str:
    """The 'teach the tech stack' block shared by Learning and Guided."""
    lang_name = _LANG_NAME[language]
    stdlib = _LANG_STDLIB[language]
    topics = list(already_learned_topics or [])
    if topics:
        skip = (
            "The learner has ALREADY studied these topics: "
            f"{', '.join(topics)}. Do NOT re-explain them — instead briefly "
            "cross-link to that prior knowledge and spend your effort on what is new."
        )
    else:
        skip = "Assume no prior topics have been studied yet (already-learned: none)."
    return (
        f"Teach, in {lang_name} (language key: {language}), the full tech stack "
        "needed to solve this problem. "
        "Cover the relevant data structures and the algorithms involved, and explain "
        "HOW to use each one in real code. "
        f"Reach for idiomatic built-ins where they help — e.g. {stdlib}. "
        f"{skip}"
    )


def _bigo_fragment() -> str:
    """The Big-O instruction required in every answer-producing prompt."""
    return (
        "You MUST include one explicit complexity line stating the Big-O time "
        "complexity AND the Big-O space complexity of the solution, e.g. "
        "`Complexity: time O(n), space O(1)`."
    )


def _answer_fragment(tier: str, language: str, *, with_tradeoff: bool) -> str:
    """The 'produce the answer + step-by-step reasoning' block.

    ``with_tradeoff`` adds the Answer-mode instruction to compare against the
    other tiers; Guided omits it (it commits to a single tier in a pipeline).
    """
    lang_name = _LANG_NAME[language]
    stdlib = _LANG_STDLIB[language]
    parts = [
        f"Produce a working {lang_name} (language key: {language}) solution at the "
        f"**{tier}** tier: {_TIER_DESC[tier]}",
        f"Where it helps, use idiomatic built-ins — e.g. {stdlib}.",
        "Walk through your reasoning step-by-step before and around the code so the "
        "learner can follow how the solution is derived.",
        _bigo_fragment(),
    ]
    if with_tradeoff:
        others = [t for t in TIERS if t != tier]
        parts.append(
            "Then call out the trade-off of this tier versus the other tiers "
            f"({' and '.join(others)}): what you gain or give up in time/space "
            "complexity, readability, and library use by choosing the "
            f"{tier} approach."
        )
    return "\n\n".join(parts)


# --- public builders -----------------------------------------------------

def build_learning(problem: str, *, language: str, already_learned_topics=None) -> str:
    """Build the Learning prompt (no tier).

    Teaches the tech stack; never asks for a final graded solution or Big-O line
    (that is Answer's job), so the two modes stay distinct.
    """
    _check_language(language)
    return "\n\n".join(
        [
            "You are a patient coding tutor.",
            _problem_block(problem),
            _teach_fragment(language, already_learned_topics),
            "Do not just hand over the final solution — focus on building "
            "understanding of the underlying techniques so the learner could solve "
            "it themselves.",
        ]
    )


def build_answer(problem: str, *, tier: str, language: str) -> str:
    """Build the Answer prompt for ``tier`` x ``language``.

    Always demands code + step-by-step reasoning + a Big-O line + the trade-off
    vs the other tiers.
    """
    _check_tier(tier)
    _check_language(language)
    return "\n\n".join(
        [
            "You are an expert competitive-programming assistant.",
            _problem_block(problem),
            _answer_fragment(tier, language, with_tradeoff=True),
        ]
    )


def build_guided(problem: str, *, tier: str, language: str) -> str:
    """Build the Guided-Learning prompt for ``tier`` x ``language``.

    One piped document: restate -> teach (Learning fragment) -> reason -> answer
    (Answer fragment). Inherits the Big-O requirement from the Answer fragment.
    """
    _check_tier(tier)
    _check_language(language)
    return "\n\n".join(
        [
            "You are a patient coding tutor running a single guided session.",
            _problem_block(problem),
            "Work through this as ONE flowing document with these stages:",
            "1) Restate the problem in your own words so the learner is oriented.",
            "2) " + _teach_fragment(language),
            "3) Reason step-by-step toward a solution.",
            "4) " + _answer_fragment(tier, language, with_tradeoff=False),
        ]
    )
