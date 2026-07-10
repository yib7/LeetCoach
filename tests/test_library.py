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
"""
from __future__ import annotations

import pytest

import app as app_module

# A string that exists verbatim in app.py — used to prove a traversal response
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
    return {
        "answers/two_pointers/two_sum__normal.py",
        "answers/two_pointers/two_sum__normal.md",
        "learning/arrays_learning/intro.md",
        "topic_index.json",
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


def test_library_file_topic_index_is_readable(client):
    # topic_index.json lives inside output and is harmless; .json is allowlisted.
    c, tmp_path = client
    _seed(tmp_path)
    resp = c.get("/library/file?path=topic_index.json")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "{}"


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
    import app as real_app

    c, tmp_path = client
    _seed(tmp_path)
    abs_target = str(real_app.HERE / "app.py")  # C:\...\app.py — a real file
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
