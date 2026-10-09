"""3.4: every text-mode file or pipe in the repo names its encoding.

Without `encoding=`, Python decodes with the locale's code page (cp1252 on a
stock Windows install), so a non-ASCII problem title or note reads back
mangled, or raises, on one machine and works on another. This scans the
tracked Python sources with `ast` instead of trusting reviewers to spot it.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _const(node):
    return node.value if isinstance(node, ast.Constant) else None


def _mode(node, index):
    mode = _const(node.args[index]) if len(node.args) > index else None
    for k in node.keywords:
        if k.arg == "mode":
            mode = _const(k.value)
    return mode


def text_io_without_encoding(source: str) -> list[int]:
    """Line numbers of text-mode open/read_text/write_text/subprocess text pipes
    that leave the encoding to the locale."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        kw = {k.arg for k in node.keywords}
        if "encoding" in kw:
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        owner = ast.unparse(fn.value) if isinstance(fn, ast.Attribute) else ""
        # os.open is a raw fd; urllib's opener.open fetches a URL, not a file.
        if name == "open" and owner not in ("os", "webbrowser") and not owner.endswith("opener"):
            path_open = isinstance(fn, ast.Attribute)
            mode = _mode(node, 0 if path_open else 1)
            if "b" not in str(mode or "") and len(node.args) <= (2 if path_open else 3):
                hits.append(node.lineno)
        elif ((name == "read_text" and not node.args)
              or (name == "write_text" and len(node.args) < 2)):
            hits.append(node.lineno)
        elif owner == "subprocess" and name in ("run", "Popen", "check_output"):
            if any(k.arg in ("text", "universal_newlines") and _const(k.value) is not False
                   for k in node.keywords):
                hits.append(node.lineno)
    return hits


def test_the_scanner_catches_each_locale_dependent_form():
    src = (
        "open(p)\nopen(p, 'w')\np.open()\np.read_text()\np.write_text('x')\n"
        "subprocess.run(a, text=True)\n"
        "open(p, 'rb')\nopen(p, encoding='utf-8')\np.read_text(encoding='utf-8')\n"
        "p.write_text('x', encoding='utf-8')\nsubprocess.run(a, capture_output=True)\n"
        "subprocess.run(a, text=True, encoding='utf-8')\nos.open(p, 0)\nopener.open(url)\n"
    )
    assert text_io_without_encoding(src) == [1, 2, 3, 4, 5, 6]


def test_every_tracked_python_file_names_its_text_encoding():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"], capture_output=True,
                             text=True, encoding="utf-8", check=True).stdout.split()
    assert tracked
    offenders = [
        f"{rel}:{line}"
        for rel in tracked
        for line in text_io_without_encoding((ROOT / rel).read_text(encoding="utf-8"))
    ]
    assert not offenders, f"text I/O without encoding=: {offenders}"
