"""A small persisted index of topics the learner has already studied (SP5).

Learning mode reads this so Claude can **skip covered tech and cross-link** the
prior note instead of re-teaching it; after a successful Learning run we record
the run's topics back into the index so the next run benefits.

Storage is a single JSON file (default ``<output_dir>/topic_index.json`` via
``config.topic_index_path()`` — gitignored). Shape::

    {
      "by_type": { "<problem_type>": ["topic_a", "topic_b", ...], ... },
      "all": ["topic_a", "topic_b", ...],     # flattened, de-duplicated, ordered
      "by_language": {                        # B22: per-language buckets
        "python": { "by_type": {...}, "all": [...] }, ...
      }
    }

The top-level ``by_type`` / ``all`` bucket is **language-agnostic**: every
index written before B22 has only that bucket, and its entries are honoured for
every language (read as-is, never rewritten or dropped). New Learning / Guided
runs record into their language's bucket, because a topic learned in Python
(``heapq``) is not "already learned" in C++ (``priority_queue``). Every topic
is sanitized on read (B21) - an index entry is replayed into later prompts, so
a legacy 4 KB "topic" must not survive the trip - and every NEW topic on write.
SP2 M5: ``record`` never rewrites what is already on disk: legacy entries stay
byte-for-byte as they were (only the touched lists get new topics appended),
and if the file is unreadable or a section it must replace is malformed, the
original is first copied once to ``<name>.pre-v1.5.bak``.

Robustness is the whole point: a missing or corrupt file must NEVER crash a
run — it just starts from an empty index. ``record`` merges new topics in (no
duplicates, insertion order preserved). All paths are read at call time so tests
can point ``LEETCOACH_TOPIC_INDEX`` at a tmp file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path

import config
import patterns

# Serializes the load->merge->save cycle in ``record()``. Flask runs
# ``threaded=True``, so two concurrent Learning requests could otherwise
# interleave loads and saves and lose one thread's merged topics (a TOCTOU
# race). This is a single-process personal tool, so an in-process lock plus an
# atomic tmp-then-rename write is sufficient (and Windows-compatible — no
# fcntl). All record() calls share this one module-level lock regardless of the
# target path; contention is trivial for a personal tool.
_RECORD_LOCK = threading.Lock()

_LANG_KEY_RE = re.compile(r"[a-z][a-z0-9+#]{0,15}")


def _path(path=None) -> Path:
    """Resolve the index path (explicit arg wins, else config default)."""
    if path is not None:
        return Path(path)
    return config.topic_index_path()


def _empty_bucket() -> dict:
    return {"by_type": {}, "all": []}


def _empty() -> dict:
    return {**_empty_bucket(), "by_language": {}}


def _clean_topics(values) -> list:
    """B21: every stored topic is sanitized (``[a-z0-9 _+-]``, <= 40 chars) on
    the way in AND out - a legacy index may hold anything."""
    return patterns.sanitize_topics(values, limit=None)


def _clean_bucket(raw) -> dict:
    """One ``{"by_type": {...}, "all": [...]}`` bucket, well-typed and
    sanitized; anything malformed degrades to empty parts."""
    raw = raw if isinstance(raw, dict) else {}
    by_type = raw.get("by_type")
    clean_by_type: dict = {}
    if isinstance(by_type, dict):
        for k, v in by_type.items():
            if isinstance(v, list):
                clean_by_type[str(k)] = _clean_topics(v)
    return {"by_type": clean_by_type, "all": _clean_topics(raw.get("all"))}


def _language_key(language) -> str | None:
    if not isinstance(language, str):
        return None
    key = language.strip().lower()
    return key if _LANG_KEY_RE.fullmatch(key) else None


def load(path=None) -> dict:
    """Load the index, returning a fresh empty dict on any problem.

    Never raises: a missing file, unreadable file, invalid JSON, or a JSON value
    of the wrong shape all degrade to an empty index (or empty parts). Always
    returns a dict with ``by_type`` (dict), ``all`` (list) and ``by_language``
    (dict of language -> bucket) present, well-typed and sanitized.
    """
    data, _existed = _read_raw(_path(path))
    return _clean_index(data)


def _read_raw(p: Path):
    """``(parsed top-level dict or None, file_existed)``. Never raises."""
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None, True
    return (data if isinstance(data, dict) else None), True


def _clean_index(data) -> dict:
    """The sanitized, well-typed view of a raw index dict (``None`` -> empty)."""
    if not isinstance(data, dict):
        return _empty()
    out = {**_clean_bucket(data), "by_language": {}}
    by_language = data.get("by_language")
    if isinstance(by_language, dict):
        for lang, bucket in by_language.items():
            key = _language_key(lang)
            if key is not None:
                out["by_language"][key] = _clean_bucket(bucket)
    return out


def save(data: dict, path=None) -> str:
    """Write ``data`` to the index file (UTF-8 JSON), creating parents. Returns the
    path written. Best-effort normalization so the file stays well-shaped."""
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = data if isinstance(data, dict) else {}
    normalized = {
        "by_type": data.get("by_type", {}),
        "all": data.get("all", []),
        "by_language": data.get("by_language", {}),
    }
    _write_json(p, normalized)
    return str(p)


def _write_json(p: Path, obj) -> None:
    # Atomic write: dump to a sibling temp file, then os.replace() onto the
    # target. os.replace is atomic on both Windows and POSIX, so a reader never
    # sees a half-written file and a crash mid-write can't corrupt the index.
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, p)


BACKUP_SUFFIX = ".pre-v1.5.bak"


def _backup_once(p: Path) -> None:
    """Copy the index to ``<name>.pre-v1.5.bak`` unless a backup already
    exists (SP2 M5). Best-effort: never raises."""
    backup = p.with_name(p.name + BACKUP_SUFFIX)
    try:
        if p.exists() and not backup.exists():
            shutil.copy2(p, backup)
    except OSError:
        pass


def _append_new(raw_list: list, incoming: list) -> list:
    """``raw_list`` untouched, plus each ``incoming`` topic whose sanitized
    form it does not already hold (legacy "Hash Map" == new "hash map")."""
    seen = {patterns.sanitize_topic(v) for v in raw_list}
    out = list(raw_list)
    for topic in incoming:
        if topic not in seen:
            seen.add(topic)
            out.append(topic)
    return out


def known_topics(path=None, *, limit=None, language=None) -> list:
    """De-duplicated list of every topic learned so far (order of first
    appearance). Empty list if nothing has been recorded / the file is missing.

    B22: with ``language``, the language-agnostic entries (every legacy,
    unkeyed entry) come first, then that language's own - never another
    language's. Without it, only the language-agnostic entries.

    ``limit`` (audit6 P2-12) keeps only the LAST ``limit`` entries. The stored
    lists preserve insertion order (first appearance), so the tail is the most
    recently first-recorded topics — i.e. "the most recent N".
    """
    # Intentionally unlocked: this is a read-only path (no lock needed for
    # correctness here) and it's safe against a concurrent record() because
    # save() writes via tmp-file + os.replace(), an atomic rename on both
    # Windows and POSIX — a reader here always sees either the old file or
    # the fully-written new one, never a partial write.
    data = load(path)
    topics = list(data["all"])
    key = _language_key(language)
    if key is not None:
        topics += data["by_language"].get(key, _empty_bucket())["all"]
    topics = _dedupe(topics)
    if limit is not None:
        topics = topics[-limit:] if limit > 0 else []
    return topics


def record(problem_type: str, topics, path=None, *, language=None) -> dict:
    """Merge ``topics`` (for ``problem_type``) into the index and persist it.

    Returns the updated index dict. New topics are appended without duplicating
    existing ones; the per-type bucket and the flat ``all`` list are both kept in
    insertion order. A blank ``problem_type`` defaults to ``"uncategorized"`` so a
    bucket always exists. B22: with ``language`` the topics go into that
    language's bucket; without it, into the language-agnostic one. Topics are
    sanitized (B21). Never raises on a write hiccup — it returns the merged
    in-memory index regardless.
    """
    ptype = (problem_type or "uncategorized").strip() or "uncategorized"
    incoming = _clean_topics(list(topics or []))
    key = _language_key(language)

    # Hold the lock across the whole load->merge->save so a concurrent record()
    # can't read a stale index between our load and save and clobber our write.
    #
    # SP2 M5: the merge works on the RAW file content, not the sanitized
    # load() view, so legacy entries are never lossily rewritten; only the two
    # touched lists get the new topics appended. Anything that has to be
    # replaced (unreadable file, a malformed section) is backed up first.
    with _RECORD_LOCK:
        p = _path(path)
        raw, existed = _read_raw(p)
        lossy = existed and raw is None
        if raw is None:
            raw = {}
        raw.setdefault("by_type", {})
        raw.setdefault("all", [])
        if key is None:
            bucket = raw
        else:
            langs = raw.get("by_language")
            if not isinstance(langs, dict):
                lossy = lossy or "by_language" in raw
                langs = raw["by_language"] = {}
            lang_key = key if key in langs else next(
                (k for k in langs if _language_key(k) == key), key)
            bucket = langs.get(lang_key)
            if not isinstance(bucket, dict):
                lossy = lossy or lang_key in langs
                bucket = langs[lang_key] = {}
        by_type = bucket.get("by_type")
        if not isinstance(by_type, dict):
            lossy = lossy or "by_type" in bucket
            by_type = bucket["by_type"] = {}
        current = by_type.get(ptype)
        if not isinstance(current, list):
            lossy = lossy or ptype in by_type
            current = []
        by_type[ptype] = _append_new(current, incoming)
        flat = bucket.get("all")
        if not isinstance(flat, list):
            lossy = lossy or "all" in bucket
            flat = []
        bucket["all"] = _append_new(flat, incoming)

        if lossy:
            _backup_once(p)
        try:
            _write_json(p, raw)
        except OSError:
            # Persisting is best-effort; the caller still gets the merged view.
            pass
        return _clean_index(raw)


def _dedupe(items) -> list:
    """De-duplicate preserving first-seen order."""
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
