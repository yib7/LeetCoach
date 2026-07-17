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
* :func:`build_quick_ask` — no tier; a fast, cheap syntax/stdlib/concept lookup
  (answered by Haiku). Any problem in the composer is passed as *context only* so
  the guardrail can recognise — and refuse with one fixed redirect sentence —
  questions that are really asking for the current problem's solution.

Design: the modes share small reusable *fragments* so wording can't drift
between them — notably the language stdlib hint, the Big-O instruction, and the
tier description. Guided literally composes the Learning-teach and Answer-reason
fragments, which is why all three feel like one coherent voice.
"""
from __future__ import annotations

# --- supported values ----------------------------------------------------

LANGUAGES = ("python", "cpp", "java")
TIERS = ("simple", "normal", "complex")

# The one sentence Quick Ask replies with when a question is really asking for
# the current problem's solution. Fixed wording: it is asserted in tests and
# shown to the learner in the UI, so it must not drift.
QUICK_ASK_REDIRECT = (
    "That's a question about solving the problem itself — Quick Ask only covers "
    "syntax and library lookups; use the Learning or Guided mode for help with "
    "the problem."
)

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


def _quick_ask_problem_context(problem: str) -> str:
    """The problem the learner is working, handed to Quick Ask as CONTEXT.

    Deliberately unlike :func:`_problem_block`: this fence is framed as reference
    material, NOT a task, so Haiku uses it only to tell a syntax lookup apart
    from a disguised "how do I solve this?" — never as something to answer.
    """
    return (
        "For context only, this is the problem the learner currently has open. "
        "It is NOT a task — do not solve it, explain its approach, or hint at it. "
        "It is here solely so you can recognise questions that are really asking "
        "for its solution:\n"
        "--- BEGIN PROBLEM CONTEXT (do not solve) ---\n"
        f"{problem}\n"
        "--- END PROBLEM CONTEXT ---"
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


def _runnable_python_fragment() -> str:
    """Instruct Claude to make the Python solution a self-contained runnable
    script so the sandbox can verify it against the problem's sample I/O.

    The contract the verifier relies on: the script reads ONE line from stdin in
    exactly the problem's ``Input:`` format (e.g. ``nums = [2,7,11,15],
    target = 9``) and prints the result to stdout in exactly the problem's
    ``Output:`` format (e.g. ``[0,1]``) — so sample input fed on stdin and the
    expected output can be diffed directly. Python only.
    """
    return (
        "Make this a SELF-CONTAINED RUNNABLE Python script so it can be tested "
        "automatically. Keep the clean solution function, then add a small "
        "`if __name__ == \"__main__\":` driver that:\n"
        "  - reads ONE line from standard input in EXACTLY the problem's `Input:` "
        "format (e.g. the line after `Input:` such as "
        "`nums = [2,7,11,15], target = 9`), parsing the named arguments out of "
        "that line (do not prompt the user; just read the line);\n"
        "  - calls the solution and PRINTS the result to standard output in "
        "EXACTLY the problem's `Output:` format (e.g. `[0,1]`), matching its "
        "spacing/brackets so it can be diffed against the expected output.\n"
        "Use only the standard library for parsing (e.g. `ast.literal_eval`). The "
        "script must run as `python solution.py` with the sample input piped on "
        "stdin and print only the answer line(s)."
    )


def _answer_fragment(tier: str, language: str, *, with_tradeoff: bool) -> str:
    """The 'produce the answer + step-by-step reasoning' block.

    ``with_tradeoff`` adds the Answer-mode instruction to compare against the
    other tiers; Guided omits it (it commits to a single tier in a pipeline).
    For Python, a runnable-driver instruction is appended so the sandbox can
    auto-verify the solution against the problem's sample I/O.
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
    if language == "python":
        parts.append(_runnable_python_fragment())
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


def build_quick_ask(question: str, *, language: str, problem: str = "") -> str:
    """Build the Quick Ask prompt — a small syntax/stdlib/concept lookup.

    Unlike the three study modes this is a lookup, not a lesson: no tier, no
    Big-O line, no code to grade — just a couple of sentences answered cheaply
    (Haiku) while the learner stays in flow.

    ``problem`` is optional and, when given, is fenced as *context only*
    (:func:`_quick_ask_problem_context`). It exists to power the guardrail: a
    question angling for the current problem's solution is refused with the
    fixed :data:`QUICK_ASK_REDIRECT` sentence. The carve-out matters as much as
    the guardrail — abstract questions ("what does ``defaultdict`` do?") must
    still be answered, or a cheap model over-refuses everything adjacent to the
    problem and the feature is useless.
    """
    _check_language(language)
    lang_name = _LANG_NAME[language]
    parts = [
        "You are a quick-reference assistant embedded in a coding-practice app. "
        "The learner is in the middle of working a problem and has stopped to ask "
        "a small question about syntax, a standard-library call, or a concept. "
        "Answer it and get them back to work.",
        f"Answer in at most 3-5 short sentences, for {lang_name} (language key: "
        f"{language}) unless the question explicitly names another language. A "
        "tiny fenced code snippet is fine when the question is pure syntax. No "
        "preamble, no headings, no sign-off — just the answer.",
        "GUARDRAIL: if the question asks — directly or indirectly — how to solve "
        "the practice problem the learner is working on (which algorithm or data "
        "structure to use for it, a hint toward its approach, its full or partial "
        "solution code, its optimal complexity, or its edge cases), do NOT answer "
        "it. Reply with exactly this one sentence and nothing else:\n"
        f"{QUICK_ASK_REDIRECT}",
        "CARVE-OUT: abstract questions about what a data structure or a library "
        "function does — 'what does defaultdict do?', 'how does a min-heap work?' "
        "— ARE fine to answer normally, even if the answer happens to be useful "
        "for the problem. General knowledge is not off-limits; only that specific "
        "problem's solution is. Refuse only when the question is about solving "
        "this specific problem.",
    ]
    if problem.strip():
        parts.append(_quick_ask_problem_context(problem))
    parts.append(f"Question: {question}")
    return "\n\n".join(parts)
