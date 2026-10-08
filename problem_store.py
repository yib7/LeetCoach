"""Problem records and the append-only run log (SP6 / D1).

Hidden app metadata under the library root (``output/.leetcoach/``, which the
library listing / read / delete routes never expose - any dot-prefixed path
segment is hidden):

    .leetcoach/problems/<problem_id>.json   one record per problem
    .leetcoach/runs.jsonl                    one JSON object per saved run

A **problem record** is ``{id, number, title, difficulty, pattern, statement,
created, updated, notes, review: {box, due, history[]}, runs[]}`` where
``runs`` lists the library-relative paths every saved run of it produced.
Records are rewritten through :func:`fsutil.atomic_write_text`.

A **run-log entry** is ``{ts, problem_id, mode, language, tier, model,
verdict, files, session_id, duration_s, pattern}``. The log is append-only:
each record is ONE line, written under a lock (a thread lock plus an OS file
lock, so a second process can't interleave), flushed and fsynced. The reader
is tolerant - a torn last line (a crash mid-write) or any other unparsable
line is skipped, and the next append starts on a fresh line.

``problem_id`` = ``<number>-<slug>`` when a number is parsed from the paste,
else the slug (:func:`storage.slug`: NFKD transliteration, hash suffix when
nothing ASCII survives). Number / title / difficulty come from the paste,
locally - no network.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import config
import fsutil
import patterns
import storage

META_DIR = ".leetcoach"
PROBLEMS_DIR = "problems"
RUNS_FILE = "runs.jsonl"
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

_LOCK = threading.Lock()


@dataclass(frozen=True)
class ParsedProblem:
    number: int | None
    title: str
    difficulty: str | None


def _difficulty(word: str | None) -> str | None:
    return word.capitalize() if word else None


def parse_problem(text: str) -> ParsedProblem:
    """Number, title and difficulty of a pasted problem (all best-effort).

    The title is the first non-blank line (markdown ``#`` stripped); a leading
    ``<n>.`` / ``<n>)`` / ``<n>:`` is the LeetCode number. The difficulty is a
    trailing ``(Easy)`` on the title, or a line within the next few that is
    just ``Easy`` / ``Medium`` / ``Hard`` (optionally ``Difficulty: ...``)."""
    lines = [ln.strip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ParsedProblem(None, "", None)
    first = lines[0].lstrip("#").strip()
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
        for line in lines[1 : 1 + _DIFFICULTY_SCAN]:
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


def read_runs(*, root=None) -> list[dict]:
    """Every parsable run-log entry, oldest first. Torn / garbage lines and
    non-object lines are skipped; a missing log is an empty list."""
    try:
        raw = (meta_dir(root) / RUNS_FILE).read_bytes()
    except OSError:
        return []
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


# --- problem records --------------------------------------------------------------

def _record_path(pid: str, root=None) -> Path:
    return meta_dir(root) / PROBLEMS_DIR / f"{pid}.json"


def _read_record(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_problem(pid: str, *, root=None) -> dict | None:
    if not valid_problem_id(pid):
        return None
    return _read_record(_record_path(pid, root))


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


def _preserve_corrupt(path: Path) -> None:
    if not path.exists():
        return
    try:
        path.replace(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
    except OSError:
        pass


def _upsert(meta: Path, pid: str, parsed: ParsedProblem, *, statement: str, pattern: str,
            paths: list[str], stamp: str, today) -> dict:
    path = meta / PROBLEMS_DIR / f"{pid}.json"
    rec = _read_record(path)
    if rec is None:
        _preserve_corrupt(path)
        rec = {
            "id": pid,
            "number": parsed.number,
            "title": parsed.title,
            "difficulty": parsed.difficulty,
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
        }
    else:
        for key, value in (("number", parsed.number), ("title", parsed.title),
                           ("difficulty", parsed.difficulty)):
            if value and not rec.get(key):
                rec[key] = value
        if pattern != patterns.FALLBACK or not rec.get("pattern"):
            rec["pattern"] = pattern
        if len(statement) > len(rec.get("statement") or ""):
            rec["statement"] = statement
        rec["updated"] = stamp
        rec.setdefault("notes", "")
        rec.setdefault("review", {"box": REVIEW_FIRST_BOX, "due": None, "history": []})
    runs = rec.get("runs") if isinstance(rec.get("runs"), list) else []
    for p in paths:
        if p not in runs:
            runs.append(p)
    rec["runs"] = runs
    fsutil.atomic_write_text(path, json.dumps(rec, ensure_ascii=False, indent=2) + "\n",
                             newline="\n")
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
) -> str:
    """Upsert the problem record and append one run-log line for a saved run.
    Returns the ``problem_id``."""
    base = _root(root)
    now = now or datetime.now().astimezone()
    stamp = now.isoformat(timespec="seconds")
    parsed = parse_problem(problem)
    if parsed.number is None or parsed.difficulty is None:
        doc_number, doc_difficulty = parse_doc_header(doc)
        parsed = ParsedProblem(
            parsed.number if parsed.number is not None else doc_number,
            parsed.title,
            parsed.difficulty or doc_difficulty,
        )
    # The id comes from the PASTE only (D1), so it is stable across runs.
    pid = problem_id(parse_problem(problem))
    pattern = patterns.normalize_pattern(pattern)
    files = [_rel(p, base) for p in paths]
    entry = {
        "ts": stamp,
        "problem_id": pid,
        "mode": mode,
        "language": language,
        "tier": tier or None,
        "model": model or None,
        "verdict": verdict,
        "files": files,
        "session_id": session_id or None,
        "duration_s": round(duration_s, 1) if duration_s is not None else None,
        "pattern": pattern,
    }
    meta = meta_dir(base)
    with _locked(meta):
        _upsert(meta, pid, parsed, statement=(problem or "").strip()[:STATEMENT_CAP],
                pattern=pattern, paths=files, stamp=stamp, today=now.date())
        _append_locked(meta, entry)
    return pid


def path_index(*, root=None) -> dict[str, dict]:
    """``{library-relative path: {verdict, problem_id, difficulty, mode}}``
    from the run log - the LAST entry naming a path wins (an identical re-run
    rewrites the same file). ``difficulty`` comes from the problem record."""
    index: dict[str, dict] = {}
    for entry in read_runs(root=root):
        files = entry.get("files")
        if not isinstance(files, list):
            continue
        for f in files:
            if isinstance(f, str):
                index[f] = entry
    records: dict[str, dict | None] = {}
    out = {}
    for path, entry in index.items():
        pid = entry.get("problem_id")
        if isinstance(pid, str) and pid not in records:
            records[pid] = load_problem(pid, root=root)
        rec = records.get(pid) if isinstance(pid, str) else None
        out[path] = {
            "verdict": entry.get("verdict"),
            "problem_id": pid,
            "difficulty": (rec or {}).get("difficulty"),
            "mode": entry.get("mode"),
        }
    return out
