"""Flashcards from study docs (SP7 / D10).

Every study doc ends with a ``## Flashcards`` section (the D2 contract). The
prompt asks for one bullet per card, on one line::

    - Q: <question> - A: <answer>

Models drift, so the parser is lenient. It accepts any of these, in any mix:

* ``- Q: ... - A: ...`` (also ``—`` / ``–`` / ``|`` as the separator, or no
  separator after a ``?``);
* ``**Q:** ... **A:** ...`` / ``Question: ... Answer: ...`` / numbered lists;
* the answer on its own (indented or bulleted) line under the question.

Anything that is not a question with an answer is ignored. The section ends at
the next H1/H2 heading or ``---`` rule (the app's own verification / attempt
blocks start with one).

:func:`to_tsv` writes the cards as an Anki-importable TSV: UTF-8, Anki's
``#separator`` / ``#html`` / ``#columns`` / ``#tags column`` header lines, and
any field holding a tab, newline or double quote wrapped in double quotes
with inner quotes doubled (the quoting Anki's text importer understands).
"""
from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

MAX_CARDS_PER_DOC = 50
FIELD_CAP = 1000
READ_CAP = 1024 * 1024

_SECTION_RE = re.compile(r"^[ \t]{0,3}##[ \t]+(?:\d+[.)][ \t]*)?flash[ -]?cards?\b[^\n]*$",
                         re.IGNORECASE | re.MULTILINE)
_SECTION_END_RE = re.compile(r"^[ \t]{0,3}(?:#{1,2}[ \t]|-{3,}[ \t]*$|\*{3,}[ \t]*$)",
                             re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
_ITEM_RE = re.compile(r"^[ \t]{0,3}(?:[-*+]|\d{1,3}[.)])[ \t]+")
_EMPH = r"(?:\*\*|__|\*|_)?"
_Q_LABEL_RE = re.compile(
    rf"^\s*{_EMPH}\s*(?:q|question)\s*\d*\s*[:.)]\s*{_EMPH}\s*", re.IGNORECASE)
_A_LABEL_RE = re.compile(rf"^\s*{_EMPH}\s*(?:a|answer)\s*[:.)]\s*{_EMPH}\s*", re.IGNORECASE)
# The answer label inside a one-line item: after a dash / pipe separator, a
# line break, or the question's own closing punctuation.
_A_SPLIT_RE = re.compile(
    rf"(?:\s+[-–—|]+\s*|\s*\n\s*(?:[-*+][ \t]+)?|(?<=[?.!)`*])\s+)"
    rf"{_EMPH}\s*(?:A|Answer|ANSWER)\s*[:.)]\s*{_EMPH}\s*")
_TRAIL_SEP_RE = re.compile(r"[\s\-–—|]+$")


def flashcard_section(doc: str) -> str:
    """The body of the doc's ``## Flashcards`` section ('' if none)."""
    text = (doc or "").replace("\r\n", "\n")
    m = _SECTION_RE.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    end = _SECTION_END_RE.search(rest)
    return rest[: end.start()] if end else rest


def _items(section: str) -> list[str]:
    """Group the section's lines into list items (a marker line starts one;
    indented / plain lines continue it; a blank line ends it). Code fences
    are skipped."""
    items: list[list[str]] = []
    current: list[str] | None = None
    in_fence = False
    for line in section.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            current = None
            continue
        if in_fence:
            continue
        if not line.strip():
            current = None
            continue
        m = _ITEM_RE.match(line)
        if m:
            current = [line[m.end():]]
            items.append(current)
        elif current is not None:
            current.append(line.strip())
        else:
            current = [line.strip()]
            items.append(current)
    return ["\n".join(parts).strip() for parts in items]


def _clean(text: str) -> str:
    text = _TRAIL_SEP_RE.sub("", text.strip())
    text = re.sub(r"^(?:\*\*|__)(.*)(?:\*\*|__)$", r"\1", text, flags=re.S).strip()
    for mark in ("**", "__"):  # an unbalanced wrapper left by a split label
        if text.count(mark) % 2:
            if text.endswith(mark):
                text = text[: -len(mark)].rstrip()
            elif text.startswith(mark):
                text = text[len(mark):].lstrip()
    if len(text) > FIELD_CAP:
        text = text[:FIELD_CAP].rstrip() + "…"
    return text


def parse_flashcards(doc: str) -> list[tuple[str, str]]:
    """``[(question, answer), ...]`` from the doc's ``## Flashcards``."""
    cards: list[tuple[str, str]] = []
    pending_q: str | None = None
    for item in _items(flashcard_section(doc)):
        a_only = _A_LABEL_RE.match(item)
        if a_only and pending_q is not None:
            answer = _clean(item[a_only.end():])
            if answer:
                cards.append((pending_q, answer))
            pending_q = None
            continue
        q_label = _Q_LABEL_RE.match(item)
        if not q_label:
            pending_q = None
            continue
        body = item[q_label.end():]
        split = _A_SPLIT_RE.search(body)
        if split:
            question, answer = _clean(body[: split.start()]), _clean(body[split.end():])
            if question and answer:
                cards.append((question, answer))
            pending_q = None
        else:
            pending_q = _clean(body) or None
        if len(cards) >= MAX_CARDS_PER_DOC:
            break
    return cards[:MAX_CARDS_PER_DOC]


def card_id(question: str, answer: str) -> str:
    return hashlib.sha1(f"{question}\x1f{answer}".encode()).hexdigest()[:12]


# --- collecting across the library ------------------------------------------------------

_cache: dict[str, tuple] = {}
_cache_lock = threading.Lock()
_CACHE_CAP = 4096


def _doc_cards(path: Path) -> list[tuple[str, str]]:
    try:
        st = path.stat()
    except OSError:
        return []
    key, sig = str(path), (st.st_mtime_ns, st.st_size)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            raw = fh.read(READ_CAP)
    except OSError:
        return []
    cards = parse_flashcards(raw.decode("utf-8", errors="replace"))
    with _cache_lock:
        if len(_cache) >= _CACHE_CAP:
            _cache.clear()
        _cache[key] = (sig, cards)
    return cards


def _pattern_of(rel: str) -> str:
    parts = rel.split("/")
    return re.sub(r"_learning$", "", parts[1]) if len(parts) >= 3 else ""


def collect(root: Path, files: list[dict], *, problem_id: str | None = None,
            path: str | None = None) -> list[dict]:
    """Every card in the library's Markdown docs (``files`` is the library
    listing), newest doc first; an identical question + answer seen in an
    older doc is dropped. ``problem_id`` / ``path`` narrow it to one problem
    (its record id, from the listing) or one doc."""
    docs = [f for f in files if str(f.get("path", "")).lower().endswith(".md")]
    if path is not None:
        docs = [f for f in docs if f.get("path") == path]
    if problem_id is not None:
        docs = [f for f in docs if f.get("problem_id") == problem_id]
    docs.sort(key=lambda f: (-(f.get("mtime") or 0), f.get("path", "")))
    out, seen = [], set()
    for f in docs:
        rel = f["path"]
        for question, answer in _doc_cards(root / rel):
            cid = card_id(question, answer)
            if cid in seen:
                continue
            seen.add(cid)
            out.append({
                "id": cid,
                "q": question,
                "a": answer,
                "path": rel,
                "problem_id": f.get("problem_id"),
                "title": f.get("title"),
                "number": f.get("number"),
                "pattern": _pattern_of(rel),
            })
    return out


# --- Anki TSV -------------------------------------------------------------------------

# A field a spreadsheet would read as a formula (or DDE) gets a leading ``'``
# so opening the export in Excel / Sheets never evaluates card text.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n")


def tsv_field(value: str) -> str:
    """One TSV field: quoted (inner quotes doubled) when it holds a tab, a
    line break or a double quote, or starts with ``#`` (a comment to Anki).
    A formula-like start (``= + - @``, tab, CR) is neutralized with ``'``."""
    text = str(value or "")
    if text.startswith(_FORMULA_STARTS):
        text = "'" + text
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(ch in text for ch in ("\t", "\n", '"')) or text.startswith("#"):
        return '"' + text.replace('"', '""') + '"'
    return text


def _tag(value) -> str:
    return re.sub(r"[^a-z0-9_:-]+", "_", str(value or "").lower()).strip("_")


def to_tsv(cards: list[dict]) -> str:
    lines = ["#separator:tab", "#html:false", "#columns:Front\tBack\tTags", "#tags column:3"]
    for card in cards:
        tags = ["leetcoach"] + [t for t in (_tag(card.get("pattern")),
                                            _tag(card.get("problem_id"))) if t]
        lines.append("\t".join((tsv_field(card["q"]), tsv_field(card["a"]),
                                tsv_field(" ".join(tags)))))
    return "\n".join(lines) + "\n"
