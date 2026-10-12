"""Tests for the read-only library browser (SP10).

Two endpoints, both read-only:

* ``GET /library``            — JSON listing of the study library (relative
                                paths under ``config.output_dir()``).
* ``GET /library/file?path=`` — one file's text as ``text/plain`` (never HTML,
                                so nothing the library contains can execute).

The critical property is **containment**: the ``path`` parameter is attacker
shaped (query string), so traversal (``..``, absolute paths, backslashes),
disallowed extensions, and anything else that is not a real file inside the
output root must be rejected uniformly — and the response must NEVER carry the
target file's content.

Also covered: the listing cache (it sees a nested external add/delete and
expires), app metadata hidden under any topic-index name, and
``DELETE /library/file?scope=run`` removing a run's doc and code siblings only.
The verdict field has its own file, ``test_library_verdict``.
"""
from __future__ import annotations

import pytest

from leetcoach import app as app_module
from leetcoach import claude_cli, library

# A string that exists verbatim in leetcoach/app.py — used to prove a traversal response
# never leaks the file's content, whatever the status code.
APP_PY_MARKER = "Flask web layer for LeetCoach"


def fake_run(prompt, **kwargs):  # pragma: no cover - never called by these tests
    yield "unused"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))
    application = app_module.create_app(run_fn=fake_run)
    application.config.update(TESTING=True)
    return application.test_client(), tmp_path


def _seed(tmp_path):
    """A small realistic library tree. Returns the set of expected rel paths."""
    (tmp_path / "answers" / "two_pointers").mkdir(parents=True)
    (tmp_path / "learning" / "arrays_learning").mkdir(parents=True)
    (tmp_path / "answers" / "two_pointers" / "two_sum__normal.py").write_text(
        "def two_sum():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "answers" / "two_pointers" / "two_sum__normal.md").write_text(
        "# Two Sum\n\nUse a hash map.\n", encoding="utf-8"
    )
    (tmp_path / "learning" / "arrays_learning" / "intro.md").write_text(
        "# Arrays\n", encoding="utf-8"
    )
    (tmp_path / "topic_index.json").write_text("{}", encoding="utf-8")
    # B19: app metadata is never part of the library listing.
    (tmp_path / ".leetcoach" / "problems").mkdir(parents=True)
    (tmp_path / ".leetcoach" / "problems" / "1-two-sum.json").write_text("{}", encoding="utf-8")
    return {
        "answers/two_pointers/two_sum__normal.py",
        "answers/two_pointers/two_sum__normal.md",
        "learning/arrays_learning/intro.md",
    }


# --- GET /library (listing) ------------------------------------------------

def test_library_lists_seeded_files(client):
    c, tmp_path = client
    expected = _seed(tmp_path)
    resp = c.get("/library")
    assert resp.status_code == 200
    data = resp.get_json()
    files = data["files"]
    assert {f["path"] for f in files} == expected
    # paths are relative with forward slashes, and each entry carries a size
    for f in files:
        assert not f["path"].startswith(("/", "\\"))
        assert "\\" not in f["path"]
        assert isinstance(f["size"], int) and f["size"] >= 0


def test_library_empty_dir_is_empty_list(client):
    c, _ = client
    resp = c.get("/library")
    assert resp.status_code == 200
    assert resp.get_json() == {"files": []}


def test_library_missing_dir_is_empty_list(tmp_path, monkeypatch):
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path / "does_not_exist"))
    application = app_module.create_app(run_fn=fake_run)
    application.config.update(TESTING=True)
    resp = application.test_client().get("/library")
    assert resp.status_code == 200
    assert resp.get_json() == {"files": []}


def test_library_listing_skips_disallowed_extensions(client):
    c, tmp_path = client
    _seed(tmp_path)
    (tmp_path / "evil.exe").write_bytes(b"MZ...")
    (tmp_path / "notes.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    resp = c.get("/library")
    paths = {f["path"] for f in resp.get_json()["files"]}
    assert "evil.exe" not in paths
    assert "notes.html" not in paths


# --- GET /library/file (fetch) ---------------------------------------------

def test_library_file_roundtrip(client):
    c, tmp_path = client
    _seed(tmp_path)
    resp = c.get("/library/file?path=answers/two_pointers/two_sum__normal.py")
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert "charset=utf-8" in resp.headers["Content-Type"].lower()
    assert resp.get_data(as_text=True) == "def two_sum():\n    pass\n"


def test_library_file_markdown_served_as_plain_text(client):
    c, tmp_path = client
    _seed(tmp_path)
    resp = c.get("/library/file?path=learning/arrays_learning/intro.md")
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"  # never rendered/HTML server-side
    assert resp.get_data(as_text=True) == "# Arrays\n"


def test_library_file_hides_app_metadata(client):
    # B19: topic_index.json and anything under a dot-dir (.leetcoach/) are app
    # metadata - not listed, not served, not deletable (uniform 404).
    c, tmp_path = client
    _seed(tmp_path)
    for rel in ("topic_index.json", ".leetcoach/problems/1-two-sum.json"):
        assert c.get("/library/file?path=" + rel).status_code == 404
        assert c.delete("/library/file?path=" + rel).status_code == 404
    assert (tmp_path / "topic_index.json").exists()
    assert (tmp_path / ".leetcoach" / "problems" / "1-two-sum.json").exists()


def test_library_file_missing_is_404(client):
    c, tmp_path = client
    _seed(tmp_path)
    resp = c.get("/library/file?path=answers/nope.md")
    assert resp.status_code == 404


def test_library_file_requires_path(client):
    c, _ = client
    resp = c.get("/library/file")
    assert resp.status_code == 404


# --- containment: traversal / absolute / extension attacks ------------------

TRAVERSALS = [
    "../app.py",
    "..\\..\\app.py",
    "../../LeetCoach/app.py",
    "answers/../../app.py",
    "answers\\..\\..\\app.py",
    "output/../app.py",
    "....//app.py",
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_library_file_rejects_traversal(client, path):
    c, tmp_path = client
    _seed(tmp_path)
    resp = c.get("/library/file", query_string={"path": path})
    assert resp.status_code in (403, 404)
    assert APP_PY_MARKER not in resp.get_data(as_text=True)


def test_library_file_rejects_absolute_path(client):
    from leetcoach import app as real_app

    c, tmp_path = client
    _seed(tmp_path)
    abs_target = real_app.__file__  # C:\...\leetcoach\app.py — a real file
    resp = c.get("/library/file", query_string={"path": abs_target})
    assert resp.status_code in (403, 404)
    assert APP_PY_MARKER not in resp.get_data(as_text=True)


def test_library_file_rejects_disallowed_extension(client):
    c, tmp_path = client
    (tmp_path / "evil.exe").write_bytes(b"MZ-marker-bytes")
    resp = c.get("/library/file?path=evil.exe")
    assert resp.status_code in (403, 404)
    assert "MZ-marker-bytes" not in resp.get_data(as_text=True)


def test_library_file_rejects_directory(client):
    c, tmp_path = client
    _seed(tmp_path)
    resp = c.get("/library/file?path=answers")
    assert resp.status_code in (403, 404)


# --- Host guard applies to the new routes ----------------------------------

def test_library_rejects_foreign_host(client):
    c, tmp_path = client
    _seed(tmp_path)
    resp = c.get("/library", headers={"Host": "evil.com"})
    assert resp.status_code == 403


def test_library_file_rejects_foreign_host(client):
    c, tmp_path = client
    _seed(tmp_path)
    resp = c.get(
        "/library/file?path=topic_index.json", headers={"Host": "evil.com:5000"}
    )
    assert resp.status_code == 403
    assert "{}" not in resp.get_data(as_text=True)


# --- the listing cache, hidden metadata and whole-run delete -----------------
#
# These drive a full app (with an auth probe) over an ``env`` output root.


def _authed():
    return claude_cli.AuthStatus(installed=True, logged_in=True)


class Recorder:
    """A run_fn these tests never reach: nothing here posts a run."""

    def __call__(self, prompt, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("no Claude call expected")


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(out))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(out / "topic_index.json"))
    monkeypatch.delenv("LEETCOACH_MODEL", raising=False)
    return out


def _client(run_fn):
    application = app_module.create_app(run_fn=run_fn, auth_probe=_authed)
    application.config.update(TESTING=True)
    return application.test_client()


# --- B10: library cache sees nested external changes --------------------------

def _bump_mtime(path):
    import os
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def test_library_cache_sees_a_nested_external_add_and_delete(env):
    c = _client(Recorder())
    nested = env / "answers" / "hash_map"
    nested.mkdir(parents=True)
    (nested / "a__normal.md").write_text("a", encoding="utf-8")
    assert [f["path"] for f in c.get("/library").get_json()["files"]] == [
        "answers/hash_map/a__normal.md"]

    added = nested / "b__normal.md"
    added.write_text("b", encoding="utf-8")
    _bump_mtime(nested)  # coarse-clock safety; the ROOT mtime is unchanged
    paths = [f["path"] for f in c.get("/library").get_json()["files"]]
    assert "answers/hash_map/b__normal.md" in paths

    added.unlink()
    _bump_mtime(nested)
    paths = [f["path"] for f in c.get("/library").get_json()["files"]]
    assert "answers/hash_map/b__normal.md" not in paths


def test_library_cache_expires_after_ttl(env, monkeypatch):
    calls = []
    real = library.list_files
    monkeypatch.setattr(library, "list_files", lambda root: calls.append(1) or real(root))
    c = _client(Recorder())
    c.get("/library")
    c.get("/library")
    assert len(calls) == 1
    monkeypatch.setattr(app_module, "LIBRARY_CACHE_TTL", 0.0)
    c.get("/library")
    assert len(calls) == 2


# --- B19: hidden metadata + whole-run delete ----------------------------------

def test_library_hides_metadata_even_for_a_custom_topic_index_name(env, monkeypatch):
    env.mkdir(parents=True, exist_ok=True)
    custom = env / "my_topics.json"
    custom.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(custom))
    (env / ".hidden.md").write_text("x", encoding="utf-8")
    (env / "notes.md").write_text("x", encoding="utf-8")
    c = _client(Recorder())
    assert [f["path"] for f in c.get("/library").get_json()["files"]] == ["notes.md"]
    assert c.get("/library/file?path=my_topics.json").status_code == 404
    assert c.get("/library/file?path=.hidden.md").status_code == 404


def _seed_run(root):
    folder = root / "answers" / "hash_map"
    folder.mkdir(parents=True)
    names = ["two_sum__normal.md", "two_sum__normal.py", "two_sum__normal__2.md",
             "two_sum__normal__2.py", "two_sum__basic.py", "two_sum__basic.md"]
    for n in names:
        (folder / n).write_text(n, encoding="utf-8")
    return folder


def test_delete_whole_run_removes_md_and_code_siblings_only(env):
    folder = _seed_run(env)
    c = _client(Recorder())
    c.get("/library")  # warm the cache
    resp = c.delete("/library/file?path=answers/hash_map/two_sum__normal.md&scope=run")
    assert resp.status_code == 200
    assert sorted(resp.get_json()["paths"]) == [
        "answers/hash_map/two_sum__normal.md", "answers/hash_map/two_sum__normal.py"]
    left = sorted(p.name for p in folder.iterdir())
    assert left == ["two_sum__basic.md", "two_sum__basic.py",
                    "two_sum__normal__2.md", "two_sum__normal__2.py"]
    listed = [f["path"] for f in c.get("/library").get_json()["files"]]
    assert "answers/hash_map/two_sum__normal.py" not in listed


def test_delete_single_file_is_still_the_default(env):
    folder = _seed_run(env)
    resp = _client(Recorder()).delete("/library/file?path=answers/hash_map/two_sum__normal.md")
    assert resp.status_code == 200
    assert resp.get_json()["paths"] == ["answers/hash_map/two_sum__normal.md"]
    assert (folder / "two_sum__normal.py").exists()


def test_delete_with_unknown_scope_is_rejected(env):
    _seed_run(env)
    resp = _client(Recorder()).delete(
        "/library/file?path=answers/hash_map/two_sum__normal.md&scope=everything")
    assert resp.status_code == 400
