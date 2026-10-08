"""Tests for ``fsutil`` — the one shared atomic-write helper (SP4).

Storage, the topic index, the ``.env`` model picker and (later) the problem
store all write through ``fsutil.atomic_write_text``: a sibling temp file, then
``os.replace`` onto the target, retried with backoff when Windows refuses the
replace because another process (AV, OneDrive, an open reader) holds the file,
and with the temp file always cleaned up on failure.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

import fsutil


def _tmp_leftovers(folder):
    return [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]


def test_writes_text_and_creates_parents(tmp_path):
    target = tmp_path / "a" / "b" / "note.md"
    out = fsutil.atomic_write_text(target, "hello\nworld\n")
    assert out == target
    assert target.read_text(encoding="utf-8") == "hello\nworld\n"
    assert _tmp_leftovers(target.parent) == []


def test_overwrites_existing_file(tmp_path):
    target = tmp_path / "x.json"
    target.write_text("old", encoding="utf-8")
    fsutil.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_encoding_and_newline_are_honoured(tmp_path):
    target = tmp_path / "x.txt"
    fsutil.atomic_write_text(target, "a\nb\n", newline="\n")
    assert target.read_bytes() == b"a\nb\n"
    fsutil.atomic_write_text(target, "café", encoding="utf-8")
    assert target.read_bytes() == "café".encode()


def test_temp_name_is_hidden_sibling(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(os.fspath(src))
        return real_replace(src, dst)

    monkeypatch.setattr(fsutil, "_replace", spy)
    target = tmp_path / "note.md"
    fsutil.atomic_write_text(target, "x")
    name = os.path.basename(seen[0])
    assert os.path.dirname(seen[0]) == str(tmp_path)
    assert name.startswith(".note.md.") and name.endswith(".tmp")


def test_retries_permission_error_then_succeeds(tmp_path, monkeypatch):
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "sharing violation")
        return real_replace(src, dst)

    sleeps = []
    monkeypatch.setattr(fsutil, "_replace", flaky)
    monkeypatch.setattr(fsutil, "_sleep", sleeps.append)
    target = tmp_path / "idx.json"
    fsutil.atomic_write_text(target, "{}")
    assert calls["n"] == 3
    assert target.read_text(encoding="utf-8") == "{}"
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # backoff grows
    assert _tmp_leftovers(tmp_path) == []


def test_gives_up_after_retries_and_cleans_tmp(tmp_path, monkeypatch):
    def always_locked(src, dst):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(fsutil, "_replace", always_locked)
    monkeypatch.setattr(fsutil, "_sleep", lambda s: None)
    target = tmp_path / "idx.json"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(PermissionError):
        fsutil.atomic_write_text(target, "new", retries=4)
    assert target.read_text(encoding="utf-8") == "original"  # untouched
    assert _tmp_leftovers(tmp_path) == []


def test_non_permission_oserror_is_not_retried(tmp_path, monkeypatch):
    calls = {"n": 0}

    def broken(src, dst):
        calls["n"] += 1
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fsutil, "_replace", broken)
    monkeypatch.setattr(fsutil, "_sleep", lambda s: None)
    with pytest.raises(OSError):
        fsutil.atomic_write_text(tmp_path / "f.md", "x")
    assert calls["n"] == 1
    assert _tmp_leftovers(tmp_path) == []


def test_failed_tmp_write_cleans_up(tmp_path):
    # An unencodable body fails while writing the temp file.
    with pytest.raises(UnicodeEncodeError):
        fsutil.atomic_write_text(tmp_path / "f.txt", "€", encoding="ascii")
    assert _tmp_leftovers(tmp_path) == []
    assert not (tmp_path / "f.txt").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-sharing semantics")
def test_write_succeeds_once_a_held_reader_lets_go(tmp_path):
    """The real B7 shape: a reader holds the file open (Python opens without
    FILE_SHARE_DELETE, so os.replace onto it fails), then releases it. The
    retry/backoff loop must ride that out instead of losing the update."""
    target = tmp_path / "topic_index.json"
    target.write_text("old", encoding="utf-8")
    fh = open(target, encoding="utf-8")  # noqa: SIM115 - held on purpose
    released = threading.Event()

    def release_later():
        time.sleep(0.3)
        fh.close()
        released.set()

    threading.Thread(target=release_later, daemon=True).start()
    try:
        fsutil.atomic_write_text(target, "new")
    finally:
        released.wait(5)
        if not fh.closed:
            fh.close()
    assert target.read_text(encoding="utf-8") == "new"
    assert _tmp_leftovers(tmp_path) == []


def test_write_many_writes_all_or_nothing(tmp_path, monkeypatch):
    a, b = tmp_path / "s.py", tmp_path / "s.md"
    fsutil.atomic_write_many([(a, "code"), (b, "notes")])
    assert a.read_text(encoding="utf-8") == "code"
    assert b.read_text(encoding="utf-8") == "notes"


def test_write_many_rolls_back_new_files_when_a_later_replace_fails(tmp_path, monkeypatch):
    a, b = tmp_path / "s.py", tmp_path / "s.md"
    real_replace = os.replace

    def fail_on_md(src, dst):
        if os.fspath(dst).endswith(".md"):
            raise OSError(5, "boom")
        return real_replace(src, dst)

    monkeypatch.setattr(fsutil, "_replace", fail_on_md)
    monkeypatch.setattr(fsutil, "_sleep", lambda s: None)
    with pytest.raises(OSError):
        fsutil.atomic_write_many([(a, "code"), (b, "notes")])
    assert not a.exists() and not b.exists()  # the pair landed together or not at all
    assert _tmp_leftovers(tmp_path) == []


def test_write_many_restores_a_preexisting_file_on_rollback(tmp_path, monkeypatch):
    a, b = tmp_path / "s.py", tmp_path / "s.md"
    a.write_text("previous", encoding="utf-8")
    real_replace = os.replace

    def fail_on_md(src, dst):
        if os.fspath(dst).endswith(".md") and not os.fspath(src).endswith(".bak"):
            raise OSError(5, "boom")
        return real_replace(src, dst)

    monkeypatch.setattr(fsutil, "_replace", fail_on_md)
    monkeypatch.setattr(fsutil, "_sleep", lambda s: None)
    with pytest.raises(OSError):
        fsutil.atomic_write_many([(a, "code"), (b, "notes")])
    assert a.read_text(encoding="utf-8") == "previous"
    assert not b.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["s.py"]


# --- SP4 review M5/M6/M7 ---------------------------------------------------------

def test_worst_case_wait_matches_the_real_backoff_schedule(monkeypatch, tmp_path):
    def always_locked(src, dst):
        raise PermissionError(13, "locked")

    sleeps = []
    monkeypatch.setattr(fsutil, "_replace", always_locked)
    monkeypatch.setattr(fsutil, "_sleep", sleeps.append)
    with pytest.raises(PermissionError):
        fsutil.atomic_write_text(tmp_path / "f.md", "x")
    assert sum(sleeps) == pytest.approx(fsutil.WORST_CASE_WAIT)
    assert fsutil.WORST_CASE_WAIT == pytest.approx(3.55)


def test_temp_name_suffix_is_short(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(os.path.basename(os.fspath(src)))
        return real_replace(src, dst)

    monkeypatch.setattr(fsutil, "_replace", spy)
    fsutil.atomic_write_text(tmp_path / "note.md", "x")
    fsutil.atomic_write_text(tmp_path / "note.md", "y")
    for name in seen:
        tag = name[len(".note.md."):-len(".tmp")]
        assert len(tag) == 8 and all(c in "0123456789abcdef" for c in tag), name
    assert seen[0] != seen[1]


def test_temp_name_collision_picks_another_name(tmp_path, monkeypatch):
    tags = iter(["deadbeef", "deadbeef", "cafef00d"])
    monkeypatch.setattr(fsutil.secrets, "token_hex", lambda n: next(tags))
    (tmp_path / ".note.md.deadbeef.tmp").write_text("someone else's", encoding="utf-8")
    fsutil.atomic_write_text(tmp_path / "note.md", "mine")
    assert (tmp_path / "note.md").read_text(encoding="utf-8") == "mine"
    # the existing temp file is not clobbered
    assert (tmp_path / ".note.md.deadbeef.tmp").read_text(encoding="utf-8") == "someone else's"


def _make_readonly(path):
    import stat

    os.chmod(path, stat.S_IREAD)


def test_read_only_target_fails_fast_without_retrying(tmp_path, monkeypatch):
    import stat

    target = tmp_path / "locked.md"
    target.write_text("original", encoding="utf-8")
    _make_readonly(target)
    sleeps = []
    monkeypatch.setattr(fsutil, "_sleep", sleeps.append)
    try:
        with pytest.raises(PermissionError, match="read-only"):
            fsutil.atomic_write_text(target, "new")
        assert sleeps == []  # no 3.5 s of pointless retries
        assert target.read_text(encoding="utf-8") == "original"
        assert not (os.stat(target).st_mode & stat.S_IWUSR)  # flag left alone
        assert _tmp_leftovers(tmp_path) == []
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)


def test_write_many_checks_read_only_before_touching_anything(tmp_path):
    import stat

    free = tmp_path / "a.md"
    locked = tmp_path / "b.md"
    locked.write_text("original", encoding="utf-8")
    _make_readonly(locked)
    try:
        with pytest.raises(PermissionError, match="read-only"):
            fsutil.atomic_write_many([(free, "a"), (locked, "b")])
        assert not free.exists()
        assert locked.read_text(encoding="utf-8") == "original"
        assert _tmp_leftovers(tmp_path) == []
    finally:
        os.chmod(locked, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows hidden attribute")
def test_hidden_attribute_survives_the_replace(tmp_path):
    import ctypes
    import stat

    target = tmp_path / "hidden.json"
    target.write_text("{}", encoding="utf-8")
    kernel32 = ctypes.windll.kernel32
    assert kernel32.SetFileAttributesW(str(target), stat.FILE_ATTRIBUTE_HIDDEN)
    fsutil.atomic_write_text(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'
    assert os.stat(target).st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN
