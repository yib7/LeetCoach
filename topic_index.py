"""A small persisted index of topics the learner has already studied (SP5).

Learning mode reads this so Claude can **skip covered tech and cross-link** the
prior note instead of re-teaching it; after a successful Learning run we record
the run's topics back into the index so the next run benefits.

Storage is a single JSON file (default ``<output_dir>/topic_index.json`` via
``config.topic_index_path()`` — gitignored). Shape::

    {
      "by_type": { "<problem_type>": ["topic_a", "topic_b", ...], ... },
      "all": ["topic_a", "topic_b", ...]      # flattened, de-duplicated, ordered
    }

Robustness is the whole point: a missing or corrupt file must NEVER crash a
run — it just starts from an empty index. ``record`` merges new topics in (no
duplicates, insertion order preserved). All paths are read at call time so tests
can point ``LEETCOACH_TOPIC_INDEX`` at a tmp file.
"""
from __future__ import annotations

import json
from pathlib import Path

import config


def _path(path=None) -> Path:
    """Resolve the index path (explicit arg wins, else config default)."""
    if path is not None:
        return Path(path)
    return config.topic_index_path()


def _empty() -> dict:
    return {"by_type": {}, "all": []}


def load(path=None) -> dict:
    """Load the index, returning a fresh empty dict on any problem.

    Never raises: a missing file, unreadable file, invalid JSON, or a JSON value
    of the wrong shape all degrade to an empty index. Always returns a dict with
    ``by_type`` (dict) and ``all`` (list) keys present and well-typed.
    """
    p = _path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return _empty()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()

    by_type = data.get("by_type")
    if not isinstance(by_type, dict):
        by_type = {}
    # sanitize: keys -> str, values -> list[str]
    clean_by_type: dict = {}
    for k, v in by_type.items():
        if isinstance(v, list):
            clean_by_type[str(k)] = [str(t) for t in v if isinstance(t, (str, int))]

    all_topics = data.get("all")
    if not isinstance(all_topics, list):
        all_topics = []
    clean_all = _dedupe(str(t) for t in all_topics if isinstance(t, (str, int)))

    return {"by_type": clean_by_type, "all": clean_all}


def save(data: dict, path=None) -> str:
    """Write ``data`` to the index file (UTF-8 JSON), creating parents. Returns the
    path written. Best-effort normalization so the file stays well-shaped."""
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    normalized = {
        "by_type": data.get("by_type", {}) if isinstance(data, dict) else {},
        "all": data.get("all", []) if isinstance(data, dict) else [],
    }
    p.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    return str(p)


def known_topics(path=None) -> list:
    """Flattened, de-duplicated list of every topic learned so far (order of first
    appearance). Empty list if nothing has been recorded / the file is missing."""
    return list(load(path).get("all", []))


def record(problem_type: str, topics, path=None) -> dict:
    """Merge ``topics`` (for ``problem_type``) into the index and persist it.

    Returns the updated index dict. New topics are appended without duplicating
    existing ones; the per-type bucket and the flat ``all`` list are both kept in
    insertion order. A blank ``problem_type`` defaults to ``"uncategorized"`` so a
    bucket always exists. Never raises on a write hiccup — it returns the merged
    in-memory index regardless.
    """
    data = load(path)
    ptype = (problem_type or "uncategorized").strip() or "uncategorized"
    incoming = [str(t).strip() for t in (topics or []) if str(t).strip()]

    bucket = list(data["by_type"].get(ptype, []))
    data["by_type"][ptype] = _dedupe(bucket + incoming)
    data["all"] = _dedupe(list(data["all"]) + incoming)

    try:
        save(data, path)
    except OSError:
        # Persisting is best-effort; the caller still gets the merged view.
        pass
    return data


def _dedupe(items) -> list:
    """De-duplicate preserving first-seen order."""
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
