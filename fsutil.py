"""Shared atomic file writes (SP4).

Every file LeetCoach owns (study docs, the topic index, the model picker's
``.env``, and the SP6 problem store / run log) is written through
:func:`atomic_write_text`:

* the body goes to a hidden sibling temp file (``.<name>.<8 hex>.tmp``, created
  exclusively so two writers never share one; same directory so ``os.replace``
  is a same-volume rename), flushed and fsynced;
* ``os.replace`` then swaps it onto the target, so a reader sees either the old
  file or the complete new one, never a partial write;
* on Windows ``os.replace`` fails with ``PermissionError`` while another process
  (antivirus, OneDrive, an open reader) holds the target without
  ``FILE_SHARE_DELETE`` (B7). That is retried with a growing backoff instead of
  silently losing the update (worst case :data:`WORST_CASE_WAIT`, ~3.5 s);
* a READ-ONLY target is refused up front with a clear ``PermissionError``
  (no retries, the flag is never cleared): read-only means "don't change this";
* a Windows hidden target stays hidden (the attribute is copied onto the temp
  file before the swap). ACLs and other attributes are NOT carried over - the
  new file gets its directory's defaults, like any freshly created file;
* on any failure the temp file is removed, so no ``*.tmp`` litter is left.

:func:`atomic_write_many` writes a group of files (an Answer's code + notes
pair) all-or-nothing.

``_replace`` and ``_sleep`` are module attributes so tests can simulate a locked
target deterministically.
"""
from __future__ import annotations

import errno
import os
import secrets
import stat
import time
from pathlib import Path

_replace = os.replace
_sleep = time.sleep

DEFAULT_RETRIES = 8
DEFAULT_BACKOFF = 0.05  # seconds; doubles per attempt, capped at _MAX_BACKOFF
_MAX_BACKOFF = 1.0

_FILE_ATTRIBUTE_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2)


def _backoff_schedule(retries: int, backoff: float) -> list[float]:
    """The sleeps :func:`_replace_with_retry` makes before giving up."""
    delays, delay = [], backoff
    for _ in range(max(retries - 1, 0)):
        delays.append(delay)
        delay = min(delay * 2, _MAX_BACKOFF)
    return delays


# Longest a write waits on a held target before raising (defaults: ~3.55 s).
WORST_CASE_WAIT = sum(_backoff_schedule(DEFAULT_RETRIES, DEFAULT_BACKOFF))


def _open_tmp(target: Path):
    """Create a fresh hidden temp sibling ``.<name>.<8 hex>.tmp`` exclusively
    (``O_EXCL``: a name that already exists is never reused) and return
    ``(path, fd)``."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    while True:
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
        try:
            return tmp, os.open(tmp, flags, 0o666)
        except FileExistsError:
            continue


def _refuse_read_only(target: Path) -> None:
    """Raise ``PermissionError`` at once if ``target`` exists and is read-only
    (no write bit / the Windows read-only attribute). On Windows a replace onto
    it would fail with "access denied" eight times over ~3.5 s; on POSIX it
    would silently succeed. Either way the user marked it "don't change", so
    we refuse clearly and never clear the flag."""
    try:
        mode = os.stat(target).st_mode
    except OSError:
        return
    if stat.S_ISREG(mode) and not mode & stat.S_IWUSR:
        raise PermissionError(
            errno.EACCES,
            "file is read-only; clear its read-only flag to let LeetCoach update it",
            str(target),
        )


def _hidden_attributes(target: Path) -> int:
    """``FILE_ATTRIBUTE_HIDDEN`` if ``target`` is a hidden file on Windows,
    else 0."""
    try:
        attrs = getattr(os.stat(target), "st_file_attributes", 0)
    except OSError:
        return 0
    return attrs & _FILE_ATTRIBUTE_HIDDEN


def _set_attributes(path: Path, extra: int) -> None:
    """Best-effort: add Windows file attribute bits ``extra`` to ``path``."""
    if not extra or os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        current = kernel32.GetFileAttributesW(str(path))
        if current != 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
            kernel32.SetFileAttributesW(str(path), current | extra)
    except (OSError, AttributeError):
        pass


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _write_tmp(target: Path, data: bytes) -> Path:
    """Write ``data`` to a fresh hidden temp sibling of ``target``; return it.
    Cleans the temp file up itself if the write fails."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp, fd = _open_tmp(target)
    try:
        with open(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # Carry a hidden target's attribute over BEFORE the swap, so the
        # replaced file is never visible-then-hidden.
        _set_attributes(tmp, _hidden_attributes(target))
    except BaseException:
        _unlink_quietly(tmp)
        raise
    return tmp


def _replace_with_retry(tmp: Path, target: Path, *, retries: int, backoff: float) -> None:
    """``os.replace(tmp, target)``, retrying ``PermissionError`` (a held file
    on Windows) up to ``retries`` times with exponential backoff. Any other
    ``OSError`` fails immediately. The caller owns ``tmp`` cleanup."""
    delay = backoff
    attempt = 0
    while True:
        try:
            _replace(tmp, target)
            return
        except PermissionError:
            attempt += 1
            if attempt >= retries:
                raise
            _sleep(delay)
            delay = min(delay * 2, _MAX_BACKOFF)


def _encode(text: str, encoding: str, newline: str | None) -> bytes:
    # ``newline=None`` mirrors ``Path.write_text``: "\n" becomes os.linesep.
    if newline is None:
        newline = os.linesep
    if newline != "\n":
        text = text.replace("\n", newline)
    return text.encode(encoding)


def atomic_write_bytes(
    path,
    data: bytes,
    *,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> Path:
    """Atomically replace ``path`` with ``data``; return the target ``Path``."""
    target = Path(path)
    _refuse_read_only(target)
    tmp = _write_tmp(target, data)
    try:
        _replace_with_retry(tmp, target, retries=retries, backoff=backoff)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    return target


def atomic_write_text(
    path,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> Path:
    """Atomically replace ``path`` with ``text``; return the target ``Path``.

    ``newline=None`` (the default) translates ``"\\n"`` to the platform line
    ending exactly like ``Path.write_text``, so files written before this
    helper existed compare equal on re-read.
    """
    return atomic_write_bytes(
        path, _encode(text, encoding, newline), retries=retries, backoff=backoff
    )


def atomic_write_many(
    items,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> list[Path]:
    """Write several ``(path, text)`` files all-or-nothing.

    Every temp file is written first (nothing visible changes if any of those
    fails), then each is swapped into place. If a later swap fails, the files
    already swapped are rolled back: a target that did not exist is removed,
    one that did gets its previous bytes back. Returns the target paths.
    """
    targets = [(Path(p), _encode(t, encoding, newline)) for p, t in items]
    for target, _ in targets:
        _refuse_read_only(target)
    tmps: list[Path] = []
    try:
        for target, data in targets:
            tmps.append(_write_tmp(target, data))
    except BaseException:
        for tmp in tmps:
            _unlink_quietly(tmp)
        raise

    done: list[tuple[Path, bytes | None]] = []
    try:
        for (target, _), tmp in zip(targets, tmps):
            try:
                previous = target.read_bytes() if target.exists() else None
            except OSError:
                previous = None
            _replace_with_retry(tmp, target, retries=retries, backoff=backoff)
            done.append((target, previous))
    except BaseException:
        for tmp in tmps:
            _unlink_quietly(tmp)
        for target, previous in reversed(done):
            try:
                if previous is None:
                    target.unlink()
                else:
                    atomic_write_bytes(target, previous, retries=retries, backoff=backoff)
            except OSError:
                pass  # best effort; the original error is what the caller sees
        raise
    return [target for target, _ in targets]
