"""CREDITS.md must name the versions actually vendored under static/vendor/.

Each vendored build carries its own version in its header comment; the credits
table has to match it, so a library bump cannot leave stale attribution behind.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "static" / "vendor"


def _head(name: str) -> str:
    return (VENDOR / name).read_text(encoding="utf-8")[:400]


def _credits_row(file_cell: str) -> str:
    text = (ROOT / "CREDITS.md").read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.startswith("| ") and file_cell in ln.split("|")[1]]
    assert len(rows) == 1, f"expected one CREDITS.md row for {file_cell}, got {rows}"
    return rows[0]


def _version_cell(row: str) -> str:
    return row.split("|")[3].strip()


def test_marked_version_matches_the_vendored_build():
    m = re.search(r"marked v(\d+\.\d+\.\d+)", _head("marked.min.js"))
    assert m, "marked.min.js header lost its version line"
    assert _version_cell(_credits_row("`marked.min.js`")) == m.group(1)


def test_highlight_js_core_and_grammars_share_the_credited_version():
    m = re.search(r"Highlight\.js v(\d+\.\d+\.\d+)", _head("highlight.min.js"))
    assert m, "highlight.min.js header lost its version line"
    version = m.group(1)
    for grammar in ("hljs-python.min.js", "hljs-cpp.min.js", "hljs-java.min.js"):
        g = re.search(r"compiled for Highlight\.js (\d+\.\d+\.\d+)", _head(grammar))
        assert g and g.group(1) == version, f"{grammar} is not built for {version}"
    assert _version_cell(_credits_row("`highlight.min.js`")) == version
    assert _version_cell(_credits_row("`highlight-github-dark.min.css`")) == version


def test_every_vendored_file_is_credited():
    text = (ROOT / "CREDITS.md").read_text(encoding="utf-8")
    for f in sorted(VENDOR.iterdir()):
        assert f"`{f.name}`" in text, f"{f.name} is vendored but not listed in CREDITS.md"
