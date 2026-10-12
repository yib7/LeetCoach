"""The study library as the web layer sees it: which files under ``output/``
are listed, served and deleted, and the verdict each saved doc shows.

* :func:`list_files` - the ``/library`` listing (allowlisted extensions,
  app metadata hidden, links out of the root skipped), with each run's
  verdict / difficulty / title from the run log, or parsed from a legacy doc.
* :func:`resolve_file` - the containment gate every read and delete goes
  through: a request path resolves inside the root or not at all.
* :func:`signature` - a cheap freshness key the app's listing cache uses.

Writing the library is :mod:`leetcoach.storage`'s job; the problem records
and run log are :mod:`leetcoach.problem_store`'s.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from leetcoach import config, problem_store

# Extensions the library browser will list and serve. Everything the
# app itself writes (storage._LANG_EXT + .md, .txt fallback, topic_index.json)
# is covered; anything else in the output dir is invisible to the read path.
EXTENSIONS = frozenset({".md", ".py", ".cpp", ".java", ".txt", ".json"})


def _topic_index_resolved() -> Path | None:
    try:
        return config.topic_index_path().resolve()
    except OSError:
        return None


def _is_hidden(root: Path, path: Path, topic_index_file: Path | None = None) -> bool:
    """B19: True for app metadata that is never part of the library - any
    path with a dot-prefixed segment (``.leetcoach/``, temp files), the topic
    index (``topic_index.json`` or wherever ``LEETCOACH_TOPIC_INDEX`` points
    inside the output dir). ``path`` is inside ``root``; pass the resolved
    topic-index path when checking many files (else it is looked up)."""
    rel = path.relative_to(root)
    if any(part.startswith(".") for part in rel.parts):
        return True
    if rel.as_posix() == "topic_index.json":
        return True
    if topic_index_file is None:
        topic_index_file = _topic_index_resolved()
    return path == topic_index_file


def signature(root: Path) -> tuple:
    """B10: a cheap freshness signature for the listing - the mtime of the
    root AND of every (non-hidden) directory under it. Adding, removing or
    renaming a file anywhere bumps its parent directory's mtime, so a nested
    change made outside the app (Explorer, git, a sync client) invalidates the
    cache too, not just a top-level one."""
    sig = []
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            sig.append((str(folder), folder.stat().st_mtime_ns))
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except OSError:
            sig.append((str(folder), None))
    out = tuple(sorted(sig, key=lambda item: item[0]))
    # The hidden metadata store (run log + problem records) feeds
    # each file's verdict / difficulty, so an append made outside the app
    # (another instance, a hand edit) must invalidate the listing too.
    try:
        meta = problem_store.store_signature(root=root)
    except Exception:  # noqa: BLE001 - a signature must never fail the listing
        meta = None
    return out + (("<meta>", meta),)


# B18: the sandbox verdict a saved doc recorded in its trailing
# "**Verification:** <line>" (written by /run for Answer and Guided). Keyed by
# path + (mtime_ns, size) so an unchanged file is read once per process.
# Only the block /run itself appends ("---", blank line,
# "**Verification:** <line>") counts - a "**Verification:**" line the MODEL
# wrote inside a doc (e.g. a Learning doc quoting the format) is not a verdict.
_VERIFICATION_LINE_RE = re.compile(
    r"(?:^|\n)---\n\n\*\*Verification:\*\*[ \t]*([^\n]+?)[ \t]*(?=\n|$)"
)
_SAMPLE_VERDICT_RE = re.compile(r"Sample tests (PASS|FAIL|ERROR)\b")
_FOLLOW_UP_RE = re.compile(r"^##[ \t]+Follow-up\b", re.MULTILINE | re.IGNORECASE)
_VERDICT_READ_CAP = 1024 * 1024
_verdict_cache: dict = {}
_verdict_lock = threading.Lock()


def verdict_from_text(text: str) -> str | None:
    """``pass`` / ``fail`` / ``error`` / ``not_verified`` from the app-written
    ``---`` / ``**Verification:**`` block in ``text``, or ``None`` when it has
    none (Learning docs, legacy files). Mirrors
    :func:`leetcoach.app._verification_line`.

    Anything under a ``## Follow-up`` heading that comes AFTER the
    first verification block is a later addition to the saved run and never
    changes its verdict; the last block before that point wins."""
    text = (text or "").replace("\r\n", "\n")
    first = _VERIFICATION_LINE_RE.search(text)
    if first is None:
        return None
    follow_up = _FOLLOW_UP_RE.search(text, first.end())
    if follow_up is not None:
        text = text[: follow_up.start()]
    matches = _VERIFICATION_LINE_RE.findall(text)
    m = _SAMPLE_VERDICT_RE.search(matches[-1])
    if m:
        return m.group(1).lower()
    return "not_verified"


def verdict_from_line(line: str | None) -> str | None:
    """``pass`` / ``fail`` / ``error`` / ``not_verified`` for a
    :func:`leetcoach.app._verification_line`, ``None`` when the run was not
    verified."""
    if line is None:
        return None
    m = _SAMPLE_VERDICT_RE.search(line)
    return m.group(1).lower() if m else "not_verified"


def _log_index(root: Path) -> dict:
    """Path -> {verdict, problem_id, difficulty} from the run log;
    empty (doc fallback everywhere) if the log can't be read."""
    try:
        return problem_store.path_index(root=root)
    except Exception:  # noqa: BLE001 - the listing must never fail on metadata
        return {}


def _md_verdict(path: Path, stat) -> str | None:
    key = str(path)
    sig = (stat.st_mtime_ns, stat.st_size)
    with _verdict_lock:
        hit = _verdict_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_VERDICT_READ_CAP)
    except OSError:
        return None
    verdict = verdict_from_text(raw.decode("utf-8", errors="replace"))
    with _verdict_lock:
        _verdict_cache[key] = (sig, verdict)
    return verdict


def inside_root(path: Path, resolved_root: Path) -> bool:
    """Whether ``path`` - with every symlink / junction on the way
    followed - still lies inside ``resolved_root`` (an already-resolved
    library root). ``rglob`` descends into a junction and ``is_file()``
    follows a symlink, so without this a link inside ``output/`` would list,
    read and export a file from anywhere. Unresolvable paths count as
    outside. The same rule :func:`resolve_file` applies to reads."""
    try:
        return path.resolve().is_relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return False


def list_files(root: Path) -> list[dict]:
    """The library listing: every allowlisted file under ``root``, as
    ``{"path": <relative, forward slashes>, "size": <bytes>, "mtime": <epoch
    seconds>}`` dicts, sorted by path. ``mtime`` lets the frontend show real
    saved dates and derive the recent-runs list; callers that read only
    ``path``/``size`` can ignore it. A
    missing/empty root is an empty list, never an error. App metadata
    (``.leetcoach/``, the topic index) is left out (B19)."""
    if not root.is_dir():
        return []
    files = []
    topic_index_file = _topic_index_resolved()
    log_index = _log_index(root)
    try:
        resolved_root = root.resolve()
    except OSError:
        return []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        if _is_hidden(root, path, topic_index_file):
            continue
        if not inside_root(path, resolved_root):
            continue  # a symlink / junction pointing out of output/
        try:
            stat = path.stat()
        except OSError:
            continue  # vanished mid-walk: skip, never 500 a listing
        entry = {
            "path": path.relative_to(root).as_posix(),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }
        logged = log_index.get(entry["path"])
        if logged is not None:
            # The run log is the source of truth for a run it recorded.
            if logged.get("difficulty"):
                entry["difficulty"] = logged["difficulty"]
            if logged.get("problem_id"):
                entry["problem_id"] = logged["problem_id"]
            # The record's own title / number for display.
            if logged.get("title"):
                entry["title"] = logged["title"]
            if isinstance(logged.get("number"), int):
                entry["number"] = logged["number"]
            if path.suffix.lower() == ".md" and logged.get("verdict"):
                entry["verdict"] = logged["verdict"]
        elif path.suffix.lower() == ".md":
            # B18 (legacy files the log never saw): parsed from the doc.
            verdict = _md_verdict(path, stat)
            if verdict:
                entry["verdict"] = verdict
        files.append(entry)
    _prune_verdict_cache(root, files)
    return files


def _prune_verdict_cache(root: Path, files: list[dict]) -> None:
    """Forget cached verdicts of docs under ``root`` that the listing
    dropped (deleted / renamed), so the cache cannot grow without bound
    as runs come and go. Entries for other roots are left alone."""
    listed = {str(root / f["path"]) for f in files}
    prefix = str(root)
    with _verdict_lock:
        stale = [k for k in _verdict_cache if k.startswith(prefix) and k not in listed]
        for key in stale:
            del _verdict_cache[key]


def existing_runs(runs, listed: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """The paths of a problem record's ``runs`` that are still
    library files. The record on disk is never pruned (a delete in Explorer
    or a restored file is reflected either way); the response just stops
    pointing at docs that are gone, so counts and "show the solution" agree
    with the library.

    ``listed`` holds the paths of the (cached, freshness-keyed)
    library listing; a path in it is a library file without touching the
    disk, so only a path it lacks pays :func:`resolve_file`'s
    realpath calls."""
    if not isinstance(runs, list):
        return []
    return [p for p in runs if isinstance(p, str)
            and (p in listed or resolve_file(p) is not None)]


def resolve_file(rel: str) -> Path | None:
    """Resolve a request's ``path`` param against the output root, or ``None``.

    This is the containment gate for the library's read path, with the same rigor
    as ``storage.slug`` on the write path. ``None`` (-> a uniform 404) unless
    ALL of the following hold, so a rejection never leaks what exists:

    * non-empty, no NUL, and not absolute / drive-anchored (``C:\\...``,
      ``\\\\server\\share``, ``/etc``, ``\\foo``);
    * ``(root / rel).resolve()``, which collapses ``..`` (in slash OR
      backslash form; Windows Path treats both as separators) and follows
      symlinks, stays inside ``root.resolve()`` per ``is_relative_to``;
    * the suffix is on ``EXTENSIONS`` (checked on the RESOLVED path);
    * it is an existing regular file (directories and reserved names fail).
    """
    if not rel or "\x00" in rel:
        return None
    root = config.output_dir().resolve()
    try:
        candidate = Path(rel)
        if candidate.is_absolute() or candidate.drive:
            return None
        resolved = (root / candidate).resolve()
    except (OSError, ValueError):
        return None
    if not resolved.is_relative_to(root):
        return None
    if resolved.suffix.lower() not in EXTENSIONS:
        return None
    if not resolved.is_file():
        return None
    if _is_hidden(root, resolved):  # B19: app metadata is not a library file
        return None
    return resolved

