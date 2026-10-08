"""Problem records and the append-only run log (SP6 / D1).

Hidden app metadata under the library root (``output/.leetcoach/``, which the
library listing / read / delete routes never expose - any dot-prefixed path
segment is hidden):

    .leetcoach/problems/<problem_id>.json   one record per problem
    .leetcoach/runs.jsonl                    one JSON object per saved run

A **problem record** is ``{id, number, title, difficulty, difficulty_source,
pattern, statement, created, updated, notes, review: {box, due, history[]},
runs[], aliases[]}`` where ``runs`` lists the library-relative paths every
saved run of it produced (files, so an Answer run adds two), ``aliases`` the
older ids merged into it (an un-numbered ``two_sum`` record folds into
``1-two_sum`` once a paste gives the number) and ``difficulty_source`` is
``"paste"`` or ``"doc"`` (a doc-header guess; a later pasted difficulty
replaces it). Records are rewritten through :func:`fsutil.atomic_write_text`.
SP7: :func:`grade_problem` moves ``review`` along the Leitner boxes (D4) and
:func:`set_notes` replaces ``notes`` (D9), both under the same store lock as
a run's upsert; :func:`review_summary` is the due queue.

A **run-log entry** is ``{ts, problem_id, mode, language, tier, model,
verdict, files, session_id, duration_s, pattern}``. The log is append-only:
each record is ONE line, written under a lock (a thread lock plus an OS file
lock, so a second process can't interleave), flushed and fsynced. The reader
is tolerant - a torn last line (a crash mid-write) or any other unparsable
line is skipped, and the next append starts on a fresh line. The log is the
source of truth: a run's line is appended BEFORE its record is updated, and a
failing record write never loses the line. A run that saved no files is not
logged. ``tier`` is only kept for Answer runs (``null`` otherwise).

SP8 / D6: a follow-up question answered on a saved doc is logged too, as
``{ts, mode: "followup", problem_id, doc, resumed, session_id, model,
duration_s}`` - it names the doc in ``doc`` (never ``files``) and is NOT a
study run: :func:`read_runs` leaves follow-ups out unless asked, so Stats, run
counts, the library's path index and :func:`session_for_doc` never see them.

The parsed log and a light record index (id / number / title / difficulty -
never the statement) are cached per library root and keyed on the files'
``(mtime, size)``, so an append made outside this process is picked up on
the next read; :func:`store_signature` exposes that key for the app's
listing cache.

``problem_id`` = ``<number>-<slug>`` when a number is parsed from the paste,
else the slug (:func:`storage.slug`: NFKD transliteration, hash suffix when
nothing ASCII survives). Generic first lines (``Description``, ``Problem:``,
punctuation, ``untitled``) are skipped when looking for the title; a paste
with no usable title gets a short statement-hash suffix so unrelated
problems never share a record. Number / title / difficulty come from the
paste, locally - no network.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import config
import fsutil
import patterns
import storage

META_DIR = ".leetcoach"
PROBLEMS_DIR = "problems"
RUNS_FILE = "runs.jsonl"
FOLLOWUP_MODE = "followup"  # SP8 / D6: a follow-up log line, not a study run
LOCK_FILE = ".lock"

DIFFICULTIES = ("Easy", "Medium", "Hard")
STATEMENT_CAP = 50_000
TITLE_CAP = 120
# How many non-blank lines below the title may carry the difficulty (a
# LeetCode copy puts it 1-3 lines down, after "Solved"/"Attempted").
_DIFFICULTY_SCAN = 8

# The first Leitner box and its interval (D4): a problem enters the review
# queue due one day after its first saved run.
REVIEW_FIRST_BOX = 1
REVIEW_FIRST_INTERVAL_DAYS = 1

_NUMBERED_TITLE = re.compile(r"^(?:problem\s*)?#?(\d{1,5})\s*[.):]\s+(\S.*)$", re.IGNORECASE)
_TITLE_DIFFICULTY = re.compile(r"\s*[(\[](easy|medium|hard)[)\]]\s*$", re.IGNORECASE)
_DIFFICULTY_LINE = re.compile(
    r"^(?:difficulty\s*[:\-]?\s*)?(easy|medium|hard)\s*$", re.IGNORECASE
)
_DOC_TITLE = re.compile(r"^#\s+(\d{1,5})\.\s+\S")
_DOC_DIFFICULTY = re.compile(r"\bDifficulty:\s*(easy|medium|hard)\b", re.IGNORECASE)
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")
# M2: first lines that name no problem - skipped when looking for the title.
_GENERIC_LINE = re.compile(
    r"^(?:(?:the\s+)?problem(?:\s+(?:statement|description))?|description|question|title"
    r"|untitled|statement)\s*[:.\-]?\s*$",
    re.IGNORECASE,
)
_LABELED_TITLE = re.compile(r"^(?:problem|title|question)\s*[:\-]\s*(\S.*)$", re.IGNORECASE)
_NO_WORD = re.compile(r"^[\W_]*$")
_HASH_LEN = 6

_LOCK = threading.Lock()
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedProblem:
    number: int | None
    title: str
    difficulty: str | None
    # M2: no usable title line - the id gets ``digest`` (a statement hash).
    generic: bool = False
    digest: str = ""


def _difficulty(word: str | None) -> str | None:
    return word.capitalize() if word else None


def _generic(line: str) -> bool:
    return bool(_GENERIC_LINE.match(line)) or bool(_NO_WORD.match(line))


def parse_problem(text: str) -> ParsedProblem:
    """Number, title and difficulty of a pasted problem (all best-effort).

    The title is the first non-blank line that is not generic (markdown ``#``
    stripped; ``Description`` / ``Problem:`` / punctuation-only lines are
    skipped and a ``Problem: <title>`` label is dropped); a leading ``<n>.``
    / ``<n>)`` / ``<n>:`` is the LeetCode number. The difficulty is a
    trailing ``(Easy)`` on the title, or a line within the next few that is
    just ``Easy`` / ``Medium`` / ``Hard`` (optionally ``Difficulty: ...``).
    A paste with no usable title is ``generic`` and carries a short hash of
    its whole text (``digest``)."""
    lines = [ln.strip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ParsedProblem(None, "", None)
    start = None
    first = ""
    for i, line in enumerate(lines):
        cand = line.lstrip("#").strip()
        if _generic(cand):
            continue
        m = _LABELED_TITLE.match(cand)
        if m:
            cand = m.group(1).strip()
            if _generic(cand):
                continue
        start, first = i, cand
        break
    if start is None:
        digest = hashlib.sha1(" ".join(lines).encode("utf-8")).hexdigest()[:_HASH_LEN]
        return ParsedProblem(None, "", None, generic=True, digest=digest)
    number = None
    m = _NUMBERED_TITLE.match(first)
    if m:
        number = int(m.group(1))
        first = m.group(2).strip()
    difficulty = None
    m = _TITLE_DIFFICULTY.search(first)
    if m:
        difficulty = _difficulty(m.group(1))
        first = first[: m.start()].strip()
    if difficulty is None:
        for line in lines[start + 1 : start + 1 + _DIFFICULTY_SCAN]:
            m = _DIFFICULTY_LINE.match(line)
            if m:
                difficulty = _difficulty(m.group(1))
                break
    return ParsedProblem(number, first[:TITLE_CAP], difficulty)


def parse_doc_header(doc: str) -> tuple[int | None, str | None]:
    """``(number, difficulty)`` from a study doc's contract header (D2):
    ``# <n>. <Title>`` and ``Pattern: ... · Difficulty: <d>`` in its first
    lines. Used only where the paste itself carried none."""
    head = [ln.strip() for ln in (doc or "").replace("\r\n", "\n").split("\n")[:8]]
    number = difficulty = None
    for line in head:
        if number is None:
            m = _DOC_TITLE.match(line)
            if m:
                number = int(m.group(1))
        if difficulty is None:
            m = _DOC_DIFFICULTY.search(line)
            if m:
                difficulty = _difficulty(m.group(1))
    return number, difficulty


def problem_id(parsed: ParsedProblem) -> str:
    if parsed.generic:
        return f"untitled_{parsed.digest or '0' * _HASH_LEN}"
    base = storage.slug(parsed.title)
    return f"{parsed.number}-{base}" if parsed.number is not None else base


def valid_problem_id(value) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


# --- paths + locking --------------------------------------------------------------

def _root(root=None) -> Path:
    return Path(root) if root is not None else config.output_dir()


def meta_dir(root=None) -> Path:
    return _root(root) / META_DIR


def _rel(path, root: Path) -> str:
    """``path`` relative to the library root, forward slashes; verbatim when
    it lies outside it (never fails)."""
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


@contextlib.contextmanager
def _locked(meta: Path):
    """Exclusive lock over the metadata store: the in-process lock plus an
    OS lock on ``.leetcoach/.lock`` (a second LeetCoach process can't
    interleave a log line or lose a record update)."""
    with _LOCK:
        meta.mkdir(parents=True, exist_ok=True)
        fh = open(meta / LOCK_FILE, "a+b")
        try:
            _os_lock(fh)
            try:
                yield
            finally:
                _os_unlock(fh)
        finally:
            fh.close()


def _os_lock(fh) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        deadline = time.monotonic() + 10.0
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    else:  # pragma: no cover - exercised on POSIX CI only
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _os_unlock(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


# --- run log ----------------------------------------------------------------------

def _append_locked(meta: Path, entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(meta / RUNS_FILE, "a+b") as fh:
        fh.seek(0, os.SEEK_END)
        prefix = b""
        if fh.tell() > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                prefix = b"\n"  # a torn last line stays on its own (skipped) line
            fh.seek(0, os.SEEK_END)
        fh.write(prefix + line.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def append_run(entry: dict, *, root=None) -> None:
    """Append one run-log entry (locked, fsynced, one line)."""
    meta = meta_dir(root)
    with _locked(meta):
        _append_locked(meta, entry)


# --- caches (M4) ------------------------------------------------------------------
# Keyed on the files' (mtime_ns, size): an append or a record rewrite made by
# another process (or by hand) changes the key, so nothing stale is served.

_CACHE_CAP = 64
_cache_lock = threading.Lock()
_log_cache: dict[str, tuple] = {}     # log path -> (sig, entries)
_index_cache: dict[str, tuple] = {}   # problems dir -> (sig, light index)
_path_cache: dict[str, tuple] = {}    # meta dir -> (store sig, path index)


def _cache_put(cache: dict, key: str, value: tuple) -> None:
    with _cache_lock:
        if key not in cache and len(cache) >= _CACHE_CAP:
            cache.clear()
        cache[key] = value


def _cache_get(cache: dict, key: str, sig):
    with _cache_lock:
        hit = cache.get(key)
    return hit[1] if hit is not None and hit[0] == sig else None


def _file_sig(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _records_sig(folder: Path) -> tuple:
    items = []
    try:
        with os.scandir(folder) as entries:
            for e in entries:
                if not e.name.endswith(".json"):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                items.append((e.name, st.st_mtime_ns, st.st_size))
    except OSError:
        return ()
    return tuple(sorted(items))


def store_signature(*, root=None) -> tuple:
    """A cheap freshness key for the whole metadata store: the run log's
    ``(mtime, size)`` plus every record file's. Changes on any append or
    record rewrite, including ones made outside this process."""
    meta = meta_dir(root)
    return (_file_sig(meta / RUNS_FILE), _records_sig(meta / PROBLEMS_DIR))


def _parse_log(raw: bytes) -> list[dict]:
    entries = []
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def is_followup(entry) -> bool:
    """True for a D6 follow-up log line (not a saved study run)."""
    return isinstance(entry, dict) and entry.get("mode") == FOLLOWUP_MODE


def read_runs(*, root=None, include_followups: bool = False) -> list[dict]:
    """Every parsable run-log entry, oldest first. Torn / garbage lines and
    non-object lines are skipped; a missing log is an empty list. Cached on
    the log's ``(mtime, size)`` - treat the returned dicts as read-only.

    SP8 / D6: follow-up lines are left out unless ``include_followups`` - they
    are not runs (Stats, run counts and the path index count study runs)."""
    path = meta_dir(root) / RUNS_FILE
    sig = _file_sig(path)
    if sig is None:
        return []
    entries = _cache_get(_log_cache, str(path), sig)
    if entries is None:
        try:
            raw = path.read_bytes()
        except OSError:
            return []
        entries = tuple(_parse_log(raw))
        # ``sig`` was taken before the read: a line appended meanwhile changes
        # the key, so the next call re-reads rather than trusting this snapshot.
        _cache_put(_log_cache, str(path), (sig, entries))
    if include_followups:
        return list(entries)
    return [e for e in entries if not is_followup(e)]


def session_for_doc(rel_path: str, *, root=None) -> dict | None:
    """SP8 / D6: the most recent study-run log entry whose ``files`` include
    the library-relative ``rel_path`` (forward slashes), or ``None`` (a legacy
    doc the log never saw). Its ``session_id`` may be missing or ``None``."""
    found = None
    for entry in read_runs(root=root):
        files = entry.get("files")
        if isinstance(files, list) and rel_path in files:
            found = entry  # oldest first: the last match is the newest run
    return found


def record_followup(
    rel_path: str,
    *,
    problem_id: str | None,
    resumed: bool,
    session_id: str | None,
    model: str | None,
    duration_s: float | None,
    now: datetime | None = None,
    root=None,
) -> None:
    """SP8 / D6: append one follow-up line to the run log (locked, fsynced).
    The doc goes in ``doc``, never ``files``, so the path index and Stats'
    legacy-file grouping are unaffected."""
    now = now or datetime.now().astimezone()
    append_run({
        "ts": now.isoformat(timespec="seconds"),
        "mode": FOLLOWUP_MODE,
        "problem_id": problem_id or None,
        "doc": rel_path,
        "resumed": bool(resumed),
        "session_id": session_id or None,
        "model": model or None,
        "duration_s": round(duration_s, 1) if duration_s is not None else None,
    }, root=root)


# --- problem records --------------------------------------------------------------

_LIGHT_FIELDS = ("id", "number", "title", "difficulty")


def _record_path(pid: str, root=None) -> Path:
    return meta_dir(root) / PROBLEMS_DIR / f"{pid}.json"


def _read_record(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record_index(*, root=None) -> dict[str, dict]:
    """``{id: {id, number, title, difficulty}}`` for every record, plus each
    merged-away alias pointing at its record's entry. Light on purpose (no
    statement); cached on the record files' ``(mtime, size)``. Read-only."""
    folder = meta_dir(root) / PROBLEMS_DIR
    sig = _records_sig(folder)
    hit = _cache_get(_index_cache, str(folder), sig)
    if hit is not None:
        return hit
    index: dict[str, dict] = {}
    aliases: dict[str, dict] = {}
    for name, _, _ in sig:
        rec = _read_record(folder / name)
        if rec is None or not valid_problem_id(rec.get("id")):
            continue
        light = {k: rec.get(k) for k in _LIGHT_FIELDS}
        index[rec["id"]] = light
        for alias in rec.get("aliases") or ():
            if valid_problem_id(alias):
                aliases[alias] = light
    for alias, light in aliases.items():
        index.setdefault(alias, light)
    _cache_put(_index_cache, str(folder), (sig, index))
    return index


def load_problem(pid: str, *, root=None) -> dict | None:
    """The record ``pid`` - or the record it was merged into (an alias)."""
    if not valid_problem_id(pid):
        return None
    rec = _read_record(_record_path(pid, root))
    if rec is not None:
        return rec
    light = record_index(root=root).get(pid)
    if light and light.get("id") != pid and valid_problem_id(light.get("id")):
        return _read_record(_record_path(light["id"], root))
    return None


def list_problems(*, root=None) -> list[dict]:
    folder = meta_dir(root) / PROBLEMS_DIR
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return []
    out = []
    for path in files:
        rec = _read_record(path)
        if rec is not None and valid_problem_id(rec.get("id")):
            out.append(rec)
    return out


def runs_for(pid: str, *, root=None) -> list[dict]:
    """The run-log entries of problem ``pid``, its merged aliases included."""
    rec = load_problem(pid, root=root)
    ids = {pid}
    if rec is not None:
        ids.add(rec.get("id"))
        ids.update(a for a in rec.get("aliases") or () if isinstance(a, str))
    return [e for e in read_runs(root=root) if e.get("problem_id") in ids]


def _preserve_corrupt(path: Path) -> None:
    if not path.exists():
        return
    try:
        path.replace(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
    except OSError:
        pass


def _resolve(meta: Path, pasted: ParsedProblem) -> tuple[str, str | None]:
    """``(problem_id, id to merge into it or None)`` for a paste (M1).

    A numbered paste (``1. Two Sum``) adopts an earlier un-numbered record of
    the same slug (``two_sum``) - unless that one already carries a different
    number. An un-numbered paste joins the single numbered record with its
    slug when there is exactly one. Generic (hashed) ids never merge."""
    pid = problem_id(pasted)
    if pasted.generic:
        return pid, None
    folder = meta / PROBLEMS_DIR
    slug = storage.slug(pasted.title)
    if pasted.number is not None:
        if slug != pid and (folder / f"{slug}.json").is_file():
            old = _read_record(folder / f"{slug}.json")
            if old is not None and old.get("number") in (None, pasted.number):
                return pid, slug
        return pid, None
    if (folder / f"{pid}.json").is_file():
        return pid, None
    numbered = re.compile(r"\d{1,5}-" + re.escape(slug))
    try:
        matches = sorted(p.stem for p in folder.glob(f"*-{slug}.json")
                         if numbered.fullmatch(p.stem))
    except OSError:
        matches = []
    return (matches[0], None) if len(matches) == 1 else (pid, None)


def _aliases(rec: dict) -> list[str]:
    return [a for a in rec.get("aliases") or () if isinstance(a, str)]


def _source(rec: dict) -> str | None:
    """Where a record's difficulty came from; a legacy record without the
    field counts as a doc guess (a pasted difficulty may replace it)."""
    if not rec.get("difficulty"):
        return None
    return rec.get("difficulty_source") or "doc"


def _cap_notes(notes: str) -> str:
    """Merged notes within :data:`NOTES_CAP` (SP7 fix 7): the joined text is
    cut with a visible marker, so a later notes save does not fail the cap."""
    if len(notes) <= NOTES_CAP:
        return notes
    return notes[:NOTES_CAP - len(MERGED_NOTES_MARKER)] + MERGED_NOTES_MARKER


def _merged_review(target: dict, old: dict) -> dict:
    """The review schedule of a merged record (SP7 fix 7): the box and due
    day of whichever side was graded last (the target's on a tie or when
    neither was), with both histories joined in time order, de-duplicated
    and capped."""
    def last(review: dict) -> str:
        value = review.get("last_reviewed")
        return value if isinstance(value, str) else ""

    base = dict(old if last(old) > last(target) else target)
    if not base:
        base = {"box": REVIEW_FIRST_BOX, "due": None}
    history, seen = [], set()
    for h in list(target.get("history") or ()) + list(old.get("history") or ()):
        if not isinstance(h, dict):
            continue
        key = json.dumps(h, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            history.append(h)
    history.sort(key=lambda h: str(h.get("ts") or ""))
    base["history"] = history[-REVIEW_HISTORY_CAP:]
    return base


def _merged(target: dict | None, old: dict, pid: str, number: int | None) -> dict:
    """``old`` (an un-numbered record) folded into ``target`` (or into a new
    record ``pid`` when there is none yet)."""
    alias_list = sorted(set(_aliases(old) + [old.get("id")]) - {None, pid})
    if target is None:
        rec = dict(old)
        rec["id"] = pid
        rec["number"] = number
        rec["aliases"] = alias_list
        return rec
    rec = target
    old_runs = [p for p in old.get("runs") or () if isinstance(p, str)]
    rec["runs"] = old_runs + [p for p in rec.get("runs") or () if p not in old_runs]
    created = [c for c in (old.get("created"), rec.get("created")) if isinstance(c, str)]
    if created:
        rec["created"] = min(created)
    if len(old.get("statement") or "") > len(rec.get("statement") or ""):
        rec["statement"] = old["statement"]
    notes = [n for n in (old.get("notes"), rec.get("notes")) if isinstance(n, str) and n]
    rec["notes"] = _cap_notes("\n\n".join(notes))
    rec["review"] = _merged_review(_review_of(rec), _review_of(old))
    if (not rec.get("pattern") or rec.get("pattern") == patterns.FALLBACK) and old.get("pattern"):
        rec["pattern"] = old["pattern"]
    if old.get("difficulty") and (
        not rec.get("difficulty") or (_source(old) == "paste" and _source(rec) != "paste")
    ):
        rec["difficulty"] = old["difficulty"]
        rec["difficulty_source"] = _source(old)
    rec["aliases"] = sorted(set(_aliases(rec) + alias_list) - {pid})
    return rec


def _upsert(meta: Path, pid: str, parsed: ParsedProblem, *, merge_from: str | None,
            difficulty_source: str | None, statement: str, pattern: str,
            paths: list[str], stamp: str, today) -> dict:
    folder = meta / PROBLEMS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{pid}.json"
    rec = _read_record(path)
    if rec is None:
        _preserve_corrupt(path)
    old = _read_record(folder / f"{merge_from}.json") if merge_from else None
    if old is not None:
        rec = _merged(rec, old, pid, parsed.number)
    if rec is None:
        rec = {
            "id": pid,
            "number": parsed.number,
            "title": parsed.title,
            "difficulty": parsed.difficulty,
            "difficulty_source": difficulty_source if parsed.difficulty else None,
            "pattern": pattern,
            "statement": statement,
            "created": stamp,
            "updated": stamp,
            "notes": "",
            "review": {
                "box": REVIEW_FIRST_BOX,
                "due": (today + timedelta(days=REVIEW_FIRST_INTERVAL_DAYS)).isoformat(),
                "history": [],
            },
            "runs": [],
            "aliases": [],
        }
    else:
        for key, value in (("number", parsed.number), ("title", parsed.title)):
            if value and not rec.get(key):
                rec[key] = value
        if parsed.difficulty and (
            not rec.get("difficulty")
            or (difficulty_source == "paste" and _source(rec) != "paste")
        ):
            rec["difficulty"] = parsed.difficulty
            rec["difficulty_source"] = difficulty_source
        rec.setdefault("difficulty_source", _source(rec))
        if pattern != patterns.FALLBACK or not rec.get("pattern"):
            rec["pattern"] = pattern
        if len(statement) > len(rec.get("statement") or ""):
            rec["statement"] = statement
        rec["updated"] = stamp
        rec.setdefault("notes", "")
        rec.setdefault("review", {"box": REVIEW_FIRST_BOX, "due": None, "history": []})
        rec.setdefault("aliases", [])
    runs = rec.get("runs") if isinstance(rec.get("runs"), list) else []
    for p in paths:
        if p not in runs:
            runs.append(p)
    rec["runs"] = runs
    fsutil.atomic_write_text(path, json.dumps(rec, ensure_ascii=False, indent=2) + "\n",
                             newline="\n")
    if old is not None:
        with contextlib.suppress(OSError):
            (folder / f"{merge_from}.json").unlink()
    return rec


def record_run(
    problem: str,
    *,
    mode: str,
    language: str,
    tier: str | None,
    model: str | None,
    verdict: str | None,
    paths,
    session_id: str | None,
    duration_s: float | None,
    pattern: str,
    doc: str = "",
    now: datetime | None = None,
    root=None,
) -> str | None:
    """Append one run-log line for a saved run, then upsert its problem
    record. Returns the ``problem_id`` - or ``None`` when the run saved no
    files (nothing is logged then, M8).

    The log line goes first (M3): it is the source of truth, so a failing
    record write is logged and swallowed rather than losing the run. ``tier``
    is recorded for Answer runs only (O2)."""
    base = _root(root)
    files = [_rel(p, base) for p in paths or ()]
    if not files:
        return None
    now = now or datetime.now().astimezone()
    stamp = now.isoformat(timespec="seconds")
    pasted = parse_problem(problem)
    number, difficulty = pasted.number, pasted.difficulty
    source = "paste" if difficulty else None
    if number is None or difficulty is None:
        doc_number, doc_difficulty = parse_doc_header(doc)
        if number is None:
            number = doc_number
        if difficulty is None and doc_difficulty:
            difficulty, source = doc_difficulty, "doc"
    parsed = ParsedProblem(number, pasted.title, difficulty)
    pattern = patterns.normalize_pattern(pattern)
    meta = meta_dir(base)
    with _locked(meta):
        # The id comes from the PASTE only (D1), so it is stable across runs.
        try:
            pid, merge_from = _resolve(meta, pasted)
        except OSError:
            pid, merge_from = problem_id(pasted), None
        _append_locked(meta, {
            "ts": stamp,
            "problem_id": pid,
            "mode": mode,
            "language": language,
            "tier": (tier or None) if mode == "answer" else None,
            "model": model or None,
            "verdict": verdict,
            "files": files,
            "session_id": session_id or None,
            "duration_s": round(duration_s, 1) if duration_s is not None else None,
            "pattern": pattern,
        })
        try:
            _upsert(meta, pid, parsed, merge_from=merge_from, difficulty_source=source,
                    statement=(problem or "").strip()[:STATEMENT_CAP], pattern=pattern,
                    paths=files, stamp=stamp, today=now.date())
        except Exception:  # noqa: BLE001 - the log line already holds the run
            _log.exception("could not update the problem record %s", pid)
    return pid


def path_index(*, root=None) -> dict[str, dict]:
    """``{library-relative path: {verdict, problem_id, difficulty, mode,
    title, number}}`` from the run log - the LAST entry naming a path wins
    (an identical re-run rewrites the same file). ``difficulty`` / ``title``
    / ``number`` come from the (light) record index, ``problem_id`` is the
    record's current id (an alias resolves to the record it merged into).
    Cached on :func:`store_signature`. Read-only."""
    sig = store_signature(root=root)
    key = str(meta_dir(root))
    hit = _cache_get(_path_cache, key, sig)
    if hit is not None:
        return hit
    index: dict[str, dict] = {}
    for entry in read_runs(root=root):
        files = entry.get("files")
        if not isinstance(files, list):
            continue
        for f in files:
            if isinstance(f, str):
                index[f] = entry
    records = record_index(root=root)
    out = {}
    for path, entry in index.items():
        pid = entry.get("problem_id")
        light = records.get(pid) if isinstance(pid, str) else None
        light = light or {}
        out[path] = {
            "verdict": entry.get("verdict"),
            "problem_id": light.get("id") or pid,
            "difficulty": light.get("difficulty"),
            "mode": entry.get("mode"),
            "title": light.get("title") or None,
            "number": light.get("number"),
        }
    _cache_put(_path_cache, key, (sig, out))
    return out


# --- review queue (SP7 / D4) ------------------------------------------------------
# Leitner boxes 1..5 with intervals [1, 3, 7, 14, 30] days. A self-grade after
# a re-attempt moves the problem: ``solo`` (solved without help) -> next box
# (capped at the last), ``hints`` -> same box, ``peeked`` (looked at the
# solution / failed) -> box 1. The new due date is the LOCAL calendar day of
# the grade plus the new box's interval. Days are local (the browser on
# localhost shares the machine's clock), so 23:59 and 00:01 are different days.

REVIEW_INTERVALS = (1, 3, 7, 14, 30)
REVIEW_MAX_BOX = len(REVIEW_INTERVALS)
GRADES = ("solo", "hints", "peeked")
REVIEW_HISTORY_CAP = 200
NOTES_CAP = 20_000
# Ends notes cut to NOTES_CAP when two records merge (SP7 fix 7).
MERGED_NOTES_MARKER = "\n\n[... notes truncated at 20,000 characters when two records merged]"


def local_today(now: datetime | None = None) -> date:
    """The machine-local calendar day of ``now`` (a naive datetime counts as
    local already; an aware one is converted to the local zone)."""
    if now is None:
        return datetime.now().date()
    if now.tzinfo is None:
        return now.date()
    return now.astimezone().date()


def clamp_box(box) -> int:
    """A stored box as a valid Leitner box (1..REVIEW_MAX_BOX). Anything that
    is not an int (a hand-edited record) restarts at box 1."""
    if isinstance(box, bool) or not isinstance(box, int):
        return REVIEW_FIRST_BOX
    return min(max(box, 1), REVIEW_MAX_BOX)


def next_review(box, grade: str, today: date) -> tuple[int, date]:
    """``(new box, due date)`` after ``grade`` on ``today`` (D4)."""
    current = clamp_box(box)
    if grade == "solo":
        new = min(current + 1, REVIEW_MAX_BOX)
    elif grade == "hints":
        new = current
    elif grade == "peeked":
        new = REVIEW_FIRST_BOX
    else:
        raise ValueError(f"unknown grade {grade!r}; expected one of {GRADES}")
    return new, today + timedelta(days=REVIEW_INTERVALS[new - 1])


def parse_due(value) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _review_of(rec: dict) -> dict:
    review = rec.get("review")
    return review if isinstance(review, dict) else {}


def _update_record(pid: str, mutate, *, root=None) -> dict | None:
    """Load the record ``pid`` (an alias resolves to its record), apply
    ``mutate(rec)`` and write it back - all under the store lock, so a grade
    or a notes save never races a run's upsert. ``None`` if there is no such
    record."""
    if not valid_problem_id(pid):
        return None
    meta = meta_dir(root)
    with _locked(meta):
        current = load_problem(pid, root=root)
        if current is None or not valid_problem_id(current.get("id")):
            return None
        path = _record_path(current["id"], root)
        rec = _read_record(path)
        if rec is None:
            return None
        mutate(rec)
        fsutil.atomic_write_text(path, json.dumps(rec, ensure_ascii=False, indent=2) + "\n",
                                 newline="\n")
        return rec


def grade_problem(pid: str, grade: str, *, attempt: dict | None = None,
                  now: datetime | None = None, root=None) -> dict | None:
    """Apply a self-grade to problem ``pid``; returns ``{"id", "review",
    "previous"}`` or ``None`` when the record does not exist. Raises
    ``ValueError`` for an unknown grade. ``attempt`` (optional, already
    validated by the caller) is kept in the history entry."""
    if grade not in GRADES:
        raise ValueError(f"unknown grade {grade!r}; expected one of {GRADES}")
    now = now or datetime.now().astimezone()
    today = local_today(now)
    out: dict = {}

    def mutate(rec: dict) -> None:
        review = dict(_review_of(rec))
        previous = {"box": clamp_box(review.get("box")), "due": review.get("due")}
        box, due = next_review(review.get("box"), grade, today)
        entry = {
            "ts": now.isoformat(timespec="seconds"),
            "day": today.isoformat(),
            "grade": grade,
            "from_box": previous["box"],
            "box": box,
            "due": due.isoformat(),
        }
        if attempt:
            entry["attempt"] = attempt
        history = [h for h in review.get("history") or () if isinstance(h, dict)]
        history.append(entry)
        review.update({
            "box": box,
            "due": due.isoformat(),
            "last_reviewed": entry["ts"],
            "history": history[-REVIEW_HISTORY_CAP:],
        })
        rec["review"] = review
        out.update(previous=previous, review=review)

    rec = _update_record(pid, mutate, root=root)
    if rec is None:
        return None
    return {"id": rec["id"], **out}


def set_notes(pid: str, notes: str, *, now: datetime | None = None, root=None) -> dict | None:
    """Replace problem ``pid``'s notes (D9); ``None`` when there is no such
    record. The caller enforces :data:`NOTES_CAP`."""
    now = now or datetime.now().astimezone()

    def mutate(rec: dict) -> None:
        rec["notes"] = notes
        rec["notes_updated"] = now.isoformat(timespec="seconds")

    return _update_record(pid, mutate, root=root)


def review_summary(*, now: datetime | None = None, root=None) -> dict:
    """The review queue for the Console panel and Stats (D4).

    ``due``: every problem whose due day is today or earlier (a record with no
    valid due day counts as due), oldest due first; ``counts``: how many are
    due, how many grades were given today, and how many problems sit in each
    box. ``next_due``: the earliest due day after today (or ``None``)."""
    today = local_today(now)
    due_items = []
    by_box = {str(b): 0 for b in range(1, REVIEW_MAX_BOX + 1)}
    reviewed_today = 0
    upcoming: date | None = None
    records = list_problems(root=root)
    for rec in records:
        review = _review_of(rec)
        box = clamp_box(review.get("box"))
        by_box[str(box)] += 1
        for h in review.get("history") or ():
            if isinstance(h, dict) and h.get("day") == today.isoformat():
                reviewed_today += 1
        due = parse_due(review.get("due"))
        if due is not None and due > today:
            upcoming = due if upcoming is None or due < upcoming else upcoming
            continue
        due_items.append({
            "id": rec.get("id"),
            "number": rec.get("number"),
            "title": rec.get("title") or "",
            "difficulty": rec.get("difficulty"),
            "pattern": rec.get("pattern"),
            "box": box,
            "due": due.isoformat() if due else None,
            "overdue_days": (today - due).days if due else 0,
        })
    due_items.sort(key=lambda i: (i["due"] or "", str(i["title"]).lower(), i["id"] or ""))
    return {
        "today": today.isoformat(),
        "due": due_items,
        "next_due": upcoming.isoformat() if upcoming else None,
        "counts": {
            "due": len(due_items),
            "reviewed_today": reviewed_today,
            "scheduled": len(records),
            "by_box": by_box,
        },
    }
