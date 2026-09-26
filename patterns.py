"""The fixed LeetCode pattern list and topic hygiene (B21 / D2).

The classifier used to return a free-form ``problem_type`` slug, so the study
library fragmented into near-duplicate folders (``dp`` vs
``dynamic_programming``, ``bfs`` vs ``graph``) and every downstream feature
keyed on "the pattern" (progress by pattern, prompt contract) had nothing
stable to key on. This module is the single source of truth:

* :data:`PATTERNS` - the ~18 canonical pattern slugs (+ human labels), shared
  by the classifier prompt and (SP6) the study-doc contract;
* :func:`normalize_pattern` - maps any free-form label the model returns (or a
  legacy folder name) onto that list, falling back to :data:`FALLBACK`;
* :func:`sanitize_topic` / :func:`sanitize_topics` - topics are replayed into
  later prompts, so each is reduced to ``[a-z0-9 _+-]``, at most
  :data:`TOPIC_MAX_LEN` chars (a 4 KB "topic" was a persistent prompt
  injection), and a classification keeps at most :data:`MAX_TOPICS`.

Existing library folders with legacy free-form names are untouched: they are
still listed and read as-is; only NEW saves use the canonical slugs.
"""
from __future__ import annotations

import re
import unicodedata

FALLBACK = "uncategorized"

# (slug, label). Slugs are folder names under output/, so they never change
# once shipped; `hash_map` / `two_pointers` / `greedy` match folders that
# already exist in real libraries.
PATTERN_LABELS: tuple[tuple[str, str], ...] = (
    ("hash_map", "Arrays & Hashing"),
    ("two_pointers", "Two Pointers"),
    ("sliding_window", "Sliding Window"),
    ("stack", "Stack / Queue (incl. monotonic)"),
    ("binary_search", "Binary Search"),
    ("linked_list", "Linked List"),
    ("trees", "Trees (binary tree / BST)"),
    ("tries", "Tries"),
    ("heap", "Heap / Priority Queue"),
    ("intervals", "Intervals / Sweep Line"),
    ("greedy", "Greedy"),
    ("backtracking", "Backtracking"),
    ("graphs", "Graphs (BFS / DFS / Union-Find / Topological Sort / Shortest Path)"),
    ("dynamic_programming", "Dynamic Programming"),
    ("prefix_sum", "Prefix Sum"),
    ("bit_manipulation", "Bit Manipulation"),
    ("math", "Math & Geometry"),
    ("design", "Design (data-structure design, e.g. LRU cache)"),
)
PATTERNS: tuple[str, ...] = tuple(slug for slug, _ in PATTERN_LABELS)

TOPIC_MAX_LEN = 40
MAX_TOPICS = 8

# Ordered: the first rule with a matching keyword wins, so the more specific
# patterns come first (``binary_search_tree`` is a tree, ``prefix_tree`` a
# trie, ``priority_queue`` a heap, ``dfs_backtracking`` backtracking). A
# keyword matches whole ``_``-separated tokens of the slugged label.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("design", ("design", "lru", "lfu", "lru_cache", "data_structure_design",
                "iterator", "system_design")),
    ("tries", ("trie", "tries", "prefix_tree", "prefix_trees")),
    ("trees", ("tree", "trees", "binary_tree", "binary_trees", "bst",
               "binary_search_tree", "segment_tree", "fenwick", "fenwick_tree",
               "binary_indexed_tree", "lca", "lowest_common_ancestor",
               "tree_traversal", "inorder", "preorder", "postorder")),
    ("linked_list", ("linked_list", "linked_lists", "linkedlist", "list_node")),
    ("heap", ("heap", "heaps", "min_heap", "max_heap", "priority_queue",
              "priority_queues", "top_k", "k_way_merge", "kth_largest")),
    ("intervals", ("interval", "intervals", "merge_intervals", "sweep_line",
                   "line_sweep", "meeting_rooms")),
    ("sliding_window", ("sliding_window", "sliding_windows", "window")),
    ("two_pointers", ("two_pointer", "two_pointers", "pointers", "fast_slow",
                      "fast_and_slow", "fast_slow_pointers", "tortoise_hare")),
    ("prefix_sum", ("prefix_sum", "prefix_sums", "prefix", "cumulative_sum",
                    "running_sum", "difference_array")),
    ("binary_search", ("binary_search", "bisect", "binary_search_on_answer",
                       "lower_bound", "upper_bound")),
    ("backtracking", ("backtracking", "backtrack", "permutation", "permutations",
                      "combination", "combinations", "subset", "subsets",
                      "n_queens", "recursion", "recursive")),
    ("dynamic_programming", ("dp", "dynamic_programming", "dynamic", "memoization",
                             "memoisation", "memo", "tabulation", "knapsack",
                             "lis", "lcs", "1d_dp", "2d_dp", "kadane")),
    ("graphs", ("graph", "graphs", "bfs", "dfs", "breadth_first_search",
                "depth_first_search", "topological", "topological_sort", "toposort",
                "union_find", "disjoint_set", "dsu", "dijkstra", "bellman_ford",
                "shortest_path", "shortest_paths", "mst", "minimum_spanning_tree",
                "grid", "matrix_traversal", "flood_fill", "islands")),
    ("stack", ("stack", "stacks", "monotonic_stack", "monotonic", "queue",
               "queues", "deque", "monotonic_queue", "parentheses")),
    ("bit_manipulation", ("bit", "bits", "bit_manipulation", "bitwise", "bitmask",
                          "bitmasking", "xor")),
    ("greedy", ("greedy",)),
    ("math", ("math", "maths", "mathematics", "number_theory", "gcd", "lcm",
              "prime", "primes", "geometry", "combinatorics", "modular",
              "modular_arithmetic", "matrix", "simulation")),
    ("hash_map", ("hash", "hashing", "hash_map", "hashmap", "hash_table",
                  "hashtable", "hash_set", "hashset", "dictionary", "dict", "map",
                  "set", "array", "arrays", "arrays_hashing", "arrays_and_hashing",
                  "counting", "frequency", "frequency_count", "anagram")),
)

_SLUG_MAX = 60  # a label longer than this is not a pattern name

# SP2 M1: generic container words. Nearly every problem touches an array, so
# as a *topic* these say nothing about the technique - letting them rescue a
# label sent "sorting" + ["array", "sorting", "greedy"] to hash_map. They
# still count in the label itself ("Arrays & Hashing" is NeetCode's name for
# hash_map); only the topic rescue ignores them.
_GENERIC_TOPIC_KEYWORDS = frozenset({"array", "arrays", "set", "map", "dict", "dictionary"})


def _slug(text: str) -> str:
    """ASCII snake_case: NFKD-transliterated, lowercased, non-alphanumerics
    collapsed to ``_``. Empty string when nothing usable remains."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


def _match(slug: str, skip: frozenset = frozenset()) -> str | None:
    if not slug or len(slug) > _SLUG_MAX:
        return None
    if slug in PATTERNS:
        return slug
    tokens = slug.split("_")
    for canonical, keywords in _RULES:
        for keyword in keywords:
            if keyword in skip:
                continue
            parts = keyword.split("_")
            n = len(parts)
            if any(tokens[i:i + n] == parts for i in range(len(tokens) - n + 1)):
                return canonical
    return None


def normalize_pattern(raw, topics=()) -> str:
    """Map a free-form pattern label onto :data:`PATTERNS` (or :data:`FALLBACK`).

    ``raw`` is tried first; if it maps nowhere, each of ``topics`` is tried in
    order (a reply like ``{"problem_type": "strings", "topics": ["hash map"]}``
    still lands in ``hash_map``), ignoring generic container words (SP2 M1:
    a topic "array" is no evidence of hash_map). Never raises; non-strings are
    ignored.
    """
    if isinstance(raw, str):
        hit = _match(_slug(raw))
        if hit:
            return hit
    for topic in topics or ():
        if isinstance(topic, str):
            hit = _match(_slug(topic), skip=_GENERIC_TOPIC_KEYWORDS)
            if hit:
                return hit
    return FALLBACK


def is_pattern(value) -> bool:
    return isinstance(value, str) and value in PATTERNS


def sanitize_topic(value) -> str | None:
    """One topic reduced to ``[a-z0-9 _+-]``, whitespace collapsed, at most
    :data:`TOPIC_MAX_LEN` chars; ``None`` if nothing usable remains."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 _+\-]+", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:TOPIC_MAX_LEN].strip()
    return text or None


def sanitize_topics(values, *, limit: int | None = MAX_TOPICS) -> list[str]:
    """Sanitize, de-duplicate (order kept) and cap a topic list. Accepts any
    value; a non-list yields ``[]``."""
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for value in values:
        topic = sanitize_topic(value)
        if topic and topic not in out:
            out.append(topic)
            if limit is not None and len(out) >= limit:
                break
    return out


def prompt_list() -> str:
    """The allowed slugs, one per line with their labels, for prompts."""
    return "\n".join(f"- {slug}: {label}" for slug, label in PATTERN_LABELS)
