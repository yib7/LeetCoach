"""Tests for `prompts.py` — prompt construction per mode x tier x language.

We assert on the *instructions* the built prompt contains, not on exact wording,
so the prompts can be reworded without breaking tests as long as the contract
holds:

* the pasted problem is always embedded,
* the requested programming language is named, with a language-specific stdlib
  hint (``heapq`` for python, ``priority_queue`` for cpp, ``PriorityQueue`` for
  java),
* every ANSWER and GUIDED prompt instructs an explicit time/space Big-O line,
  and Answer additionally calls out the trade-off vs the other tiers,
* tier semantics (simple = basic/maybe sub-optimal; normal = balanced; complex
  = best time/space) are conveyed,
* Learning has no tier and teaches the tech stack, optionally skipping
  already-learned topics.
"""
from __future__ import annotations

import pytest

import prompts

PROBLEM = "Given an array nums, return indices of the two numbers adding to target."

LANGS = ["python", "cpp", "java"]
TIERS = ["simple", "normal", "complex"]

# A representative stdlib token we expect the prompt to mention per language.
STDLIB_HINT = {
    "python": "heapq",
    "cpp": "priority_queue",
    "java": "PriorityQueue",
}


def _lower(s: str) -> str:
    return s.lower()


# --- the problem is always embedded --------------------------------------

@pytest.mark.parametrize("lang", LANGS)
def test_learning_embeds_problem_and_language(lang):
    p = prompts.build_learning(PROBLEM, language=lang)
    assert PROBLEM in p
    assert lang in _lower(p)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("tier", TIERS)
def test_answer_embeds_problem_and_language(lang, tier):
    p = prompts.build_answer(PROBLEM, tier=tier, language=lang)
    assert PROBLEM in p
    assert lang in _lower(p)


# --- Big-O instruction present in every answer / guided prompt -----------

@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("tier", TIERS)
def test_answer_requires_big_o(lang, tier):
    p = _lower(prompts.build_answer(PROBLEM, tier=tier, language=lang))
    assert "big-o" in p or "big o" in p
    assert "time" in p and "space" in p
    assert "complexity" in p


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("tier", TIERS)
def test_guided_requires_big_o(lang, tier):
    p = _lower(prompts.build_guided(PROBLEM, tier=tier, language=lang))
    assert "big-o" in p or "big o" in p
    assert "complexity" in p


def test_answer_mentions_tier_tradeoff():
    # Answer mode must call out the trade-off vs the OTHER tiers.
    p = _lower(prompts.build_answer(PROBLEM, tier="normal", language="python"))
    assert "trade-off" in p or "tradeoff" in p or "trade off" in p
    # references the other tiers by name
    assert "simple" in p and "complex" in p


# --- Learning has no tier and is a separate contract ---------------------

def test_learning_has_no_tier_argument():
    import inspect
    sig = inspect.signature(prompts.build_learning)
    assert "tier" not in sig.parameters


def test_learning_teaches_stack_not_full_answer():
    p = _lower(prompts.build_learning(PROBLEM, language="python"))
    # teaches data structures / algorithms / stdlib
    assert "data structure" in p or "data structures" in p
    assert "algorithm" in p
    # Learning should NOT demand a final runnable solution / Big-O the way Answer does
    assert "big-o" not in p and "big o" not in p


def test_learning_accepts_already_learned_topics():
    topics = ["hash_map", "sliding_window"]
    p = prompts.build_learning(PROBLEM, language="python", already_learned_topics=topics)
    # the known topics are interpolated so Claude can skip them
    assert "hash_map" in p
    assert "sliding_window" in p


def test_learning_without_already_learned_topics_is_fine():
    # the arg is optional; absence must not crash or leak placeholder text
    p = prompts.build_learning(PROBLEM, language="python")
    assert "already_learned" not in p.lower()
    assert "none" in p.lower() or "{" not in p  # no dangling format placeholder


# --- language-specific stdlib hints --------------------------------------

@pytest.mark.parametrize("lang", LANGS)
def test_learning_mentions_language_stdlib_hint(lang):
    p = prompts.build_learning(PROBLEM, language=lang)
    assert STDLIB_HINT[lang] in p


@pytest.mark.parametrize("lang", LANGS)
def test_answer_mentions_language_stdlib_hint(lang):
    p = prompts.build_answer(PROBLEM, tier="complex", language=lang)
    assert STDLIB_HINT[lang] in p


# --- tier semantics conveyed ---------------------------------------------

def test_simple_tier_says_basic_maybe_suboptimal():
    p = _lower(prompts.build_answer(PROBLEM, tier="simple", language="python"))
    assert "basic" in p or "simplest" in p
    # simple tolerates sub-optimal complexity / minimal library use
    assert "sub-optimal" in p or "suboptimal" in p or "may not be optimal" in p


def test_complex_tier_says_best_time_space():
    p = _lower(prompts.build_answer(PROBLEM, tier="complex", language="python"))
    assert "best" in p or "optimal" in p
    assert "readable" in p


def test_normal_tier_says_balanced():
    p = _lower(prompts.build_answer(PROBLEM, tier="normal", language="python"))
    assert "balance" in p or "realistic" in p or "strive" in p


# --- answer-producing prompts require step-by-step reasoning -------------

def test_answer_requires_step_by_step_reasoning():
    p = _lower(prompts.build_answer(PROBLEM, tier="normal", language="python"))
    assert "step-by-step" in p or "step by step" in p or "reasoning" in p


def test_guided_composes_learning_and_answer():
    # Guided is the pipeline: restate -> teach -> reason -> answer.
    p = _lower(prompts.build_guided(PROBLEM, tier="normal", language="python"))
    # teaches (Learning fragment) ...
    assert "data structure" in p or "tech stack" in p or "teach" in p
    # ... and produces an answer with reasoning (Answer fragment)
    assert "reasoning" in p or "step" in p


# --- Python answers must be runnable scripts (so the sandbox can verify) --

@pytest.mark.parametrize("tier", TIERS)
def test_python_answer_is_runnable_script(tier):
    p = _lower(prompts.build_answer(PROBLEM, tier=tier, language="python"))
    # instructs a stdin/stdout runnable driver so the sandbox can diff samples
    assert "stdin" in p or "standard input" in p
    assert "stdout" in p or "standard output" in p
    assert "__main__" in p


def test_python_guided_answer_is_runnable_script():
    p = _lower(prompts.build_guided(PROBLEM, tier="normal", language="python"))
    assert "stdin" in p or "standard input" in p
    assert "__main__" in p


@pytest.mark.parametrize("lang", ["cpp", "java"])
def test_non_python_answer_omits_runnable_driver(lang):
    # The runnable-stdin driver instruction is Python-only (the sandbox only
    # auto-runs Python); cpp/java should not carry the __main__ stdin contract.
    p = _lower(prompts.build_answer(PROBLEM, tier="normal", language=lang))
    assert "__main__" not in p


# --- invalid inputs are rejected clearly ---------------------------------

def test_invalid_tier_raises():
    with pytest.raises((ValueError, KeyError)):
        prompts.build_answer(PROBLEM, tier="nonsense", language="python")


def test_invalid_language_raises():
    with pytest.raises((ValueError, KeyError)):
        prompts.build_answer(PROBLEM, tier="normal", language="rust")
