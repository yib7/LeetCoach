"""C7: `.env.example` must document every LEETCOACH_* setting, and the path
settings (whose defaults are computed relative to the app, not the CWD) must
be commented out rather than shown as active — copying the file verbatim used
to silently change behavior for anyone who launches from a different working
directory (see LEETCOACH_OUTPUT_DIR's docstring in config.py).
"""
from __future__ import annotations

import re
from pathlib import Path

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# Every setting the app actually reads (config.py + app.py), kept here as a
# flat list so this test fails loudly the moment source and docs drift apart.
KNOWN_SETTINGS = [
    "LEETCOACH_MODEL",
    "LEETCOACH_CLASSIFIER_MODEL",
    "LEETCOACH_QUICK_ASK_MODEL",
    "LEETCOACH_CLAUDE_BIN",
    "LEETCOACH_OUTPUT_DIR",
    "LEETCOACH_TOPIC_INDEX",
    "LEETCOACH_RUN_TIMEOUT",
    "LEETCOACH_VERIFY_TIMEOUT",
    "LEETCOACH_NO_BROWSER",
    "LEETCOACH_NO_DOTENV",
    "LEETCOACH_CLAUDE_CWD",
    "LEETCOACH_DOTENV_PATH",
]

# Settings whose default is computed relative to the app (or otherwise
# footgun-y to set verbatim) and so must ship commented out.
PATH_SETTINGS = {"LEETCOACH_OUTPUT_DIR", "LEETCOACH_TOPIC_INDEX", "LEETCOACH_CLAUDE_CWD",
                 "LEETCOACH_DOTENV_PATH"}

# Read outside the app's own settings (setup.ps1, the scripts/dev fake CLI):
# documented as comments so .env.example covers every variable the repo reads.
NON_APP_VARIABLES = ["LEETCOACH_SETUP_PYTHON_EXE", "LEETCOACH_AUTH_STATUS_TIMEOUT_MS",
                     "LEETCOACH_AUTH_LOGIN_TIMEOUT_MS", "FAKE_CLAUDE_DELAY", "FAKE_CLAUDE_FAIL",
                     "FAKE_CLAUDE_PYTHON"]


def _lines():
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def test_every_known_setting_is_documented():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    missing = [s for s in KNOWN_SETTINGS if s not in text]
    assert not missing, f"settings missing from .env.example: {missing}"


def test_path_settings_are_commented_out_not_active():
    """A path setting must appear as `# LEETCOACH_X=...`, never as a live
    `LEETCOACH_X=...` assignment (that would override the app-relative
    default the moment someone copies .env.example -> .env)."""
    for setting in PATH_SETTINGS:
        active = [
            line for line in _lines()
            if re.match(rf"^{setting}=", line)
        ]
        assert not active, f"{setting} must be commented out, found: {active}"
        commented = [
            line for line in _lines()
            if re.match(rf"^#\s*{setting}=", line)
        ]
        assert commented, f"{setting} should appear as a commented-out example"


def test_non_path_settings_stay_active():
    # regression guard: don't accidentally comment out everything
    flags = {"LEETCOACH_NO_BROWSER", "LEETCOACH_NO_DOTENV"}
    for setting in set(KNOWN_SETTINGS) - PATH_SETTINGS - flags:
        active = [line for line in _lines() if re.match(rf"^{setting}=", line)]
        assert active, f"{setting} should be an active example line"


def test_non_app_variables_are_mentioned_but_never_active():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    missing = [v for v in NON_APP_VARIABLES if v not in text]
    assert not missing, f"variables missing from .env.example: {missing}"
    for var in NON_APP_VARIABLES:
        active = [line for line in _lines() if re.match(rf"^{var}=", line)]
        assert not active, f"{var} is not an app setting and must not be active: {active}"


def test_every_variable_the_repo_reads_is_listed_here():
    """3A: derive the list from the source, so a new knob (a launcher timeout,
    a dev hook) cannot ship undocumented because nobody updated the lists."""
    import subprocess

    root = ENV_EXAMPLE.parent
    tracked = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True,
                             text=True, encoding="utf-8", check=True).stdout.split()
    seen = set()
    for rel in tracked:
        if rel.startswith("tests/") or rel.endswith((".md", ".example")):
            continue
        path = root / rel
        if path.suffix.lower() not in {".py", ".ps1", ".cmd", ".js", ".html"}:
            continue
        seen |= set(re.findall(r"\b(?:LEETCOACH|FAKE_CLAUDE)_[A-Z0-9_]+\b",
                               path.read_text(encoding="utf-8", errors="replace")))
    listed = set(KNOWN_SETTINGS) | set(NON_APP_VARIABLES)
    assert not sorted(seen - listed), f"read by the repo but not listed: {sorted(seen - listed)}"
