"""Persist study outputs into the `output/` library, with safe filenames.

The web layer calls a handful of small functions here; each writes a file and
returns the path it wrote, so the caller can show / link it.

Directory layout (from the plan):

    output/
      learning/<problem_type>_learning/<problem>.md
      guided/<problem_type>/<problem>.md
      answers/<problem_type>/<problem>__<tier>.<ext>   (+ a sibling .md)
      reviews/<problem_type>/<problem>__review.md      (SP7 Code Review)

A saved ``.md`` doc can later grow ``## Follow-up — <question>`` sections at
its end (SP8 / D6, :func:`append_followup`), written in place under the same
write lock and through the same atomic helper.

The single most important property is **containment**: a hostile problem name
like ``../../etc/passwd`` (or an absolute path, or one full of backslashes) must
never let a write escape ``config.output_dir()``. We achieve that by running
every user-supplied path segment through :func:`slug`, which strips path
separators and ``..`` entirely before they can be interpreted as a directory.
``slug`` is the only thing standing between user input and the filesystem, so it
is deliberately strict and well-tested.

Second property: **no silent overwrites** (audit6 P2-11). Re-running with
identical content is idempotent, but a target that already exists with
*different* content (a re-run producing new material, or two long titles
sharing the same 80-char slug) shifts the write to ``<stem>__2`` / ``__3`` /
... instead of clobbering. An Answer's code + reasoning pair always shifts
together, staying on one shared stem.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import unicodedata
from pathlib import Path

import config
import fsutil

# Extension chosen per answer language. Anything unknown falls back to ``.txt``
# so an unexpected language never produces a separator-bearing extension.
_LANG_EXT = {
    "python": "py",
    "cpp": "cpp",
    "java": "java",
}

# Any run of characters that is NOT a lowercase letter, digit, hyphen or
# underscore becomes a single underscore. Crucially this maps ``/``, ``\`` and
# ``.`` (so ``..``) to underscores, which is what guarantees containment.
_UNSAFE = re.compile(r"[^a-z0-9_-]+")

# Windows reserved device names — illegal as a filename even with an extension,
# so a slug must never emit one bare (the project's primary platform is Windows).
_WIN_RESERVED = (
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

# Cap a single path segment so a long input (e.g. a whole pasted problem) can't
# build a filename that blows past the OS path limit — Windows MAX_PATH is ~260.
_MAX_SLUG = 80

# Serializes resolve-slot-then-write in `_write_entry` (audit P1-2). Flask runs
# `threaded=True`, so two concurrent identical-shaped saves (same problem,
# problem_type, tier/language) could otherwise both observe "slot N is free"
# in `_resolve_slot` before either has written, and the second write would
# silently clobber the first's saved material — violating the "no silent
# overwrites" guarantee above. This mirrors `topic_index._RECORD_LOCK`: a
# single module-level lock is sufficient for a single-process personal tool
# (no per-path locking needed); contention is trivial. The lock must cover
# BOTH the slot resolution AND the write(s) as one critical section — a lock
# that only guarded `_resolve_slot` and released before writing would still
# let two threads resolve to the same free slot before either had written.
_WRITE_LOCK = threading.Lock()


def _ascii_form(ch: str) -> str:
    """The ASCII letters/digits ``ch`` transliterates to via NFKD (``é`` ->
    ``e``, full-width ``Ｔ`` -> ``T``); ``""`` when there are none (CJK)."""
    folded = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in folded if c.isalnum())


def slug(name: str) -> str:
    """Return a filesystem-safe, lowercase slug derived from ``name``.

    Guarantees (see tests):

    * lowercase; spaces and other punctuation collapse to single ``_``;
    * ``-`` and ``_`` are preserved (they are valid, readable separators);
    * path separators (``/`` ``\\``) and ``..`` are stripped — a slug can never
      contain a directory boundary or a traversal token;
    * leading/trailing separators are trimmed;
    * length is capped at ``_MAX_SLUG`` so a huge input can't overflow the OS
      path limit;
    * Windows reserved device names (``con`` / ``nul`` / ``com1`` ...) are
      suffixed so the slug is always a legal filename on Windows;
    * non-ASCII letters are transliterated through NFKD first (B25), so
      ``Café`` -> ``cafe`` and full-width ``Ｔｗｏ`` -> ``two``;
    * never empty — a title with letters but no ASCII transliteration (e.g. a
      Chinese title) yields ``untitled_<8-hex hash>`` so distinct titles no
      longer collide on one file (B25); pure punctuation yields the literal
      ``"untitled"`` so a filename always exists;
    * when MOST of the title's letters/digits have no ASCII transliteration
      (``跳跃游戏 II``), the short hash is appended to whatever ASCII survived
      (``ii_<8-hex>``), so two such titles sharing a Latin tail don't collide
      either (SP4 review M8).
    """
    raw = (name or "").strip()
    ascii_text = (
        unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    )
    s = _UNSAFE.sub("_", ascii_text.lower())
    # Collapse any accidental runs and trim separator chars off the ends so we
    # don't get names like ``_foo_`` or ``--bar``.
    s = re.sub(r"_+", "_", s)
    s = s.strip("_-")
    alnum = [ch for ch in raw if ch.isalnum()]
    lost = sum(1 for ch in alnum if not _ascii_form(ch))
    digest = ""
    if lost and lost * 2 > len(alnum):
        digest = hashlib.sha1(
            unicodedata.normalize("NFC", raw).encode("utf-8")
        ).hexdigest()[:8]
    if not s:
        s = f"untitled_{digest}" if digest else "untitled"
    elif digest:
        # Keep the hash when the cap trims: it is what tells the titles apart.
        s = f"{s[:_MAX_SLUG - 9].rstrip('_-')}_{digest}"
    if len(s) > _MAX_SLUG:
        s = s[:_MAX_SLUG].rstrip("_-") or "untitled"
    # A bare Windows device name can't be a filename even with an extension;
    # suffix it so a write never fails on the project's primary platform.
    if s in _WIN_RESERVED:
        s = f"{s}_"
    return s


def _problem_name(problem: str) -> str:
    """A short, clean filename stem for a pasted problem.

    Uses the first non-blank line — the title, for a standard LeetCode paste —
    so a full multi-line paste saves as e.g. ``two_sum.md`` instead of a name
    built from the entire description. ``slug`` caps the length either way, which
    is what stops a giant paste from overflowing the OS path limit.
    """
    for line in (problem or "").splitlines():
        if line.strip():
            return slug(line)
    return slug(problem)


def _same_content(path: Path, body: str) -> bool:
    """True iff ``path`` already holds exactly ``body`` (UTF-8 text).

    An unreadable/undecodable existing file counts as *different* — when in
    doubt we suffix rather than risk clobbering something we could not read.
    """
    try:
        return path.read_text(encoding="utf-8") == body
    except (OSError, UnicodeDecodeError):
        return False


def _resolve_slot(folder: Path, stem: str, files: list[tuple[str, str]]) -> list[Path]:
    """Pick the collision-free stem for one logical entry (audit6 P2-11).

    ``files`` is ``[(ext, body), ...]`` — one tuple per sibling file that must
    share the stem (a Learning/Guided note is one file; an Answer is a
    code + reasoning pair). Starting from the bare ``stem``, then ``<stem>__2``,
    ``<stem>__3``, ... the first slot where EVERY sibling is either absent or
    already identical wins. Resolving the whole group at once is what keeps an
    Answer pair on one stem: if either sibling collides with different content,
    both move to the next suffix together.
    """
    n = 1
    while True:
        s = stem if n == 1 else f"{stem}__{n}"
        candidates = [folder / f"{s}.{ext}" for ext, _ in files]
        if all(
            not path.exists() or _same_content(path, body)
            for path, (_, body) in zip(candidates, files)
        ):
            return candidates
        n += 1


def _write(path: Path, body: str) -> str:
    """Atomically write ``body`` to ``path`` (UTF-8), creating parents; return
    the str path. Goes through the shared :mod:`fsutil` helper (SP4) so a
    reader never sees a half-written doc and a held file is retried."""
    fsutil.atomic_write_text(path, body)
    return str(path)


def _write_entry(folder: Path, stem: str, files: list[tuple[str, str]]) -> list[str]:
    """Write one logical entry (one or more sibling files) without clobbering.

    Identical re-runs are idempotent (same paths, no duplicates); a collision
    with different content shifts the whole group to the next ``__N`` stem.
    Resolving the slot and writing to it happen inside one lock (audit P1-2)
    so the whole check-then-write is atomic under concurrent callers.
    """
    with _WRITE_LOCK:
        paths = _resolve_slot(folder, stem, files)
        if len(paths) == 1:
            return [_write(paths[0], files[0][1])]
        # B25: an Answer's code + notes pair lands together or not at all -
        # never an orphaned code file without its reasoning.
        fsutil.atomic_write_many([(path, body) for path, (_, body) in zip(paths, files)])
        return [str(path) for path in paths]


UNSORTED_DIR = "_unsorted"


def save_unsorted(body: str) -> str:
    """Last-resort save (B25): write ``body`` to
    ``output/_unsorted/<content hash>.md`` and return the path.

    The web layer calls this when the normal save raises ``OSError`` (a path
    past the OS limit, a locked or unwritable folder), so a fully streamed
    answer is never thrown away. The name is a hash of the content, so the
    same body always lands on the same file.
    """
    folder = config.output_dir() / UNSORTED_DIR
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    return _write_entry(folder, digest, [("md", body)])[0]


def save_learning(problem: str, problem_type: str, body: str) -> str:
    """Write a Learning note and return its path.

    -> ``output/learning/<problem_type>_learning/<problem>.md``
    """
    root = config.output_dir()
    folder = root / "learning" / f"{slug(problem_type)}_learning"
    return _write_entry(folder, _problem_name(problem), [("md", body)])[0]


def save_guided(problem: str, problem_type: str, body: str) -> str:
    """Write a Guided-Learning doc and return its path.

    -> ``output/guided/<problem_type>/<problem>.md``
    """
    root = config.output_dir()
    folder = root / "guided" / slug(problem_type)
    return _write_entry(folder, _problem_name(problem), [("md", body)])[0]


REVIEW_SUFFIX = "review"


def save_review(problem: str, problem_type: str, body: str) -> str:
    """Write a Code Review doc (SP7 / D5) and return its path.

    -> ``output/reviews/<problem_type>/<problem>__review.md``
    """
    root = config.output_dir()
    folder = root / "reviews" / slug(problem_type)
    return _write_entry(folder, f"{_problem_name(problem)}__{REVIEW_SUFFIX}", [("md", body)])[0]


def attempt_block(code: str, language: str) -> str:
    """The learner's attempt as a fenced Markdown block for a saved review.
    The fence is longer than any backtick run inside the code, so the code
    can never close it early (and is tagged with the plain language, never
    ``solution``)."""
    longest = max((len(m) for m in re.findall(r"`+", code or "")), default=0)
    ticks = "`" * max(3, longest + 1)
    tag = slug(language) if language in _LANG_EXT else ""
    return f"{ticks}{tag}\n{(code or '').rstrip()}\n{ticks}\n"


def save_answer(
    problem: str,
    problem_type: str,
    *,
    tier: str,
    language: str,
    code: str,
    reasoning: str,
) -> tuple[str | None, str]:
    """Write an Answer's reasoning markdown, plus a sibling code file when
    there's actually code to save.

    -> code:      ``output/answers/<problem_type>/<problem>__<tier>.<ext>``,
                   or ``None`` when ``code`` is blank (B24) — ``extract_code``
                   returns ``""`` when the response had no fenced block at
                   all, and writing a 0-byte "solution" file would silently
                   look like a real (if empty) saved answer in the library.
    -> reasoning: ``output/answers/<problem_type>/<problem>__<tier>.md``

    Returns ``(code_path, reasoning_path)``. The extension is chosen from
    ``language`` (``py`` / ``cpp`` / ``java``), defaulting to ``txt``.
    """
    root = config.output_dir()
    folder = root / "answers" / slug(problem_type)
    stem = f"{_problem_name(problem)}__{slug(tier)}"
    if not code.strip():
        reasoning_path = _write_entry(folder, stem, [("md", reasoning)])[0]
        return None, reasoning_path
    ext = _LANG_EXT.get(slug(language), "txt")
    # The pair is ONE logical entry: resolve the stem for both siblings at
    # once so a collision on either moves code AND reasoning together.
    code_path, reasoning_path = _write_entry(
        folder, stem, [(ext, code), ("md", reasoning)]
    )
    return code_path, reasoning_path


# --- follow-up answers appended to a saved doc (SP8 / D6) -----------------

FOLLOWUP_HEADING = "## Follow-up — "
FOLLOWUP_TITLE_CAP = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_TOP_HEADING = re.compile(r"^ {0,3}#{1,2}(?=[ \t]|$)")
# The verdict regex (app.verdict_from_text) keys on this exact bold label; an
# answer that quotes it - in prose OR inside a code fence (SP8 fix M1: the
# verdict regex does not know about fences) - is reworded, so an appended
# section never contains the label and can never read as a verdict block.
_VERIFICATION_LABEL = re.compile(r"\*\*Verification:\*\*")


def _fence_step(line: str, fence: str | None) -> tuple[str | None, bool]:
    """Advance the code-fence state over one line. Returns ``(fence, inside)``:
    the opening run (```` ``` ```` / ``~~~~``) still open after the line, and
    whether the line itself is fence content or a fence marker (not Markdown)."""
    if fence is None:
        m = _FENCE_OPEN.match(line)
        return (m.group(1), True) if m else (None, False)
    stripped = line.strip()
    if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
        return None, True
    return fence, True


def open_fence(text: str) -> str | None:
    """The code fence ``text`` leaves open at its end (its opening run), or
    ``None`` when every fence is closed."""
    fence = None
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        fence, _ = _fence_step(line, fence)
    return fence


def followup_title(question: str) -> str:
    """The question as a one-line heading title: control characters and line
    breaks become spaces, runs of whitespace collapse, leading ``#`` / ``>`` /
    list markers are dropped (it must not read as more markup), and the result
    is cut to :data:`FOLLOWUP_TITLE_CAP` characters with an ellipsis."""
    text = _CONTROL.sub(" ", question or "")
    text = " ".join(text.split())
    text = re.sub(r"^[#>*+\-\s]+", "", text)
    if not text:
        text = "Question"
    if len(text) > FOLLOWUP_TITLE_CAP:
        text = text[: FOLLOWUP_TITLE_CAP - 1].rstrip() + "…"
    return text


def followup_body(answer: str) -> str:
    """The answer as it goes under its heading. Outside code fences an H1/H2
    the model wrote becomes an H3, so the follow-up stays ONE section of the
    doc and can never open a new ``## Follow-up`` / ``## Flashcards`` section.
    A quoted ``**Verification:**`` label is reworded everywhere, inside code
    fences too, so it can never read as the app's verdict block (SP8 fix M1),
    and a fence the answer left open is closed so it cannot swallow the next
    follow-up's heading (SP8 fix M3)."""
    out = []
    fence = None
    for line in (answer or "").replace("\r\n", "\n").strip("\n").split("\n"):
        fence, inside = _fence_step(line, fence)
        if not inside:
            line = _TOP_HEADING.sub("###", line, count=1)
        out.append(_VERIFICATION_LABEL.sub("**Verification**:", line))
    body = "\n".join(out).rstrip()
    if fence is not None:
        body += "\n" + fence
    return body


def followup_section(question: str, answer: str) -> str:
    """The Markdown appended for one follow-up (no leading blank lines)."""
    return f"{FOLLOWUP_HEADING}{followup_title(question)}\n\n{followup_body(answer)}\n"


def append_followup(path, question: str, answer: str) -> str:
    """Append a ``## Follow-up — <question>`` section to the saved doc at
    ``path`` and return the heading line written. The read-modify-write runs
    under the library write lock and lands through the atomic helper, so a
    reader never sees half a doc and two follow-ups never lose each other.
    Raises ``FileNotFoundError`` when the doc is gone (nothing is created) and
    ``UnicodeDecodeError`` when it is not UTF-8 (nothing is written).

    SP8 fix M3: a doc that ends inside an open code fence has it closed first,
    so the heading is real Markdown. SP8 fix I1: the doc keeps its original
    modified (and access) time - a follow-up is not a new run, and Stats
    (legacy files) and the recent-runs list date a run by its mtime. The
    library cache is still invalidated by the caller, and the verdict cache
    keys on (mtime, size), which the append always changes through the size."""
    target = Path(path)
    section = followup_section(question, answer)
    with _WRITE_LOCK:
        before = target.stat()
        text = target.read_text(encoding="utf-8")
        text = text.replace("\r\n", "\n").rstrip("\n")
        fence = open_fence(text)
        if fence is not None:
            text += "\n" + fence
        fsutil.atomic_write_text(target, text + "\n\n" + section)
        try:
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        except OSError:
            pass  # the answer landed; keeping the old date is best-effort
    return section.split("\n", 1)[0]


# --- one-time tier rename migration --------------------------------------

# The tier was renamed simple/complex -> basic/optimal (normal is unchanged),
# which is also the saved-answer filename suffix. This maps a legacy suffix to
# its new value so an existing library keeps working after the rename.
_TIER_RENAMES = {"simple": "basic", "complex": "optimal"}


def _renamed_tier(filename: str) -> str | None:
    """Map a saved answer filename to its post-rename name, or ``None`` if it
    needs no change.

    Splits the stem on ``__`` — which is *only* ever the tier (and optional
    ``__N`` slot) delimiter, because :func:`slug` collapses any run of ``_`` in a
    problem name to a single ``_`` — so the tier is always the second segment.
    """
    p = Path(filename)
    parts = p.stem.split("__")
    if len(parts) < 2:
        return None
    new_tier = _TIER_RENAMES.get(parts[1])
    if new_tier is None:
        return None
    parts[1] = new_tier
    return f"{'__'.join(parts)}{p.suffix}"


def migrate_tier_suffixes(root: Path | None = None) -> list[tuple[str, str]]:
    """Rename saved answer files from the old tier suffix to the new one.

    Walks ``<root>/answers`` (default: the configured output dir) and renames
    each ``<problem>__simple*`` / ``<problem>__complex*`` file to its new tier
    token, leaving ``normal`` and every non-answer file untouched. Both siblings
    of an Answer (code + ``.md``) are renamed because each carries the suffix.

    Idempotent and non-destructive: a rename whose destination already exists is
    skipped rather than clobbering it, so this is safe to run on every startup.
    Returns the ``(old_name, new_name)`` pairs actually renamed.
    """
    base = (root if root is not None else config.output_dir()) / "answers"
    if not base.is_dir():
        return []
    renamed: list[tuple[str, str]] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        new_name = _renamed_tier(path.name)
        if new_name is None or new_name == path.name:
            continue
        dest = path.with_name(new_name)
        if dest.exists():
            continue  # never clobber; keeps repeated runs idempotent
        try:
            path.rename(dest)
        except OSError:
            continue  # a locked/vanished file must not abort the whole sweep
        renamed.append((path.name, new_name))
    return renamed
