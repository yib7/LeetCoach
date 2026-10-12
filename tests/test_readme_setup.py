"""6.1-6.4: the README's setup recipe is numbered, has no activation step,
and is exactly what CI runs on a clean runner.

The CI jobs `readme-setup-windows` and `readme-setup-posix` are the strong
version of the fresh-machine test; these checks keep their commands and the
README's in step, so rewording a step without updating CI (or the reverse)
fails here instead of silently testing something no reader runs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

RECIPES = {
    # README subsection under "## Setup" -> the CI job that runs it
    "Windows": "readme-setup-windows",
    "Linux and macOS": "readme-setup-posix",
}


def _setup_section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("\n## Setup\n")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def _recipe(name: str) -> str:
    section = _setup_section()
    start = section.index(f"\n### {name}\n")
    end = section.find("\n### ", start + 1)
    return section[start:] if end == -1 else section[start:end]


def _commands(block: str) -> list[str]:
    fences = re.findall(r"```[a-z]*\n(.*?)```", block, re.DOTALL)
    return [line.strip() for fence in fences for line in fence.splitlines() if line.strip()]


def _ci_job(job: str) -> str:
    text = CI.read_text(encoding="utf-8")
    start = text.index(f"\n  {job}:\n")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1:])
    return text[start:] if nxt is None else text[start:start + 1 + nxt.start()]


@pytest.mark.parametrize("name", RECIPES)
def test_recipe_steps_are_numbered_from_one_in_order(name):
    steps = re.findall(r"\*\*Step (\d+)", _recipe(name))
    assert steps, f"no **Step N** headings under ### {name}"
    assert steps == [str(i) for i in range(1, len(steps) + 1)]


@pytest.mark.parametrize("name", RECIPES)
def test_every_step_has_exactly_one_command_block(name):
    """6.3: each step is one concrete action shown as a code block."""
    recipe = _recipe(name)
    parts = re.split(r"\n\*\*Step \d+[^\n]*\n", recipe)[1:]
    for i, part in enumerate(parts, start=1):
        # count opening fences only (they name a language; closing ones don't)
        assert len(re.findall(r"```[a-z]+\n", part)) == 1, f"{name} Step {i}"


def test_setup_needs_no_venv_activation():
    """The default PowerShell execution policy refuses Activate.ps1 on a client
    PC, so no setup step may depend on activating the venv."""
    section = _setup_section()
    assert "Activate.ps1" not in section
    assert "bin/activate" not in section


def test_windows_recipe_uses_the_one_launcher():
    commands = _commands(_recipe("Windows"))
    assert ".\\LeetCoach.cmd" in commands


@pytest.mark.parametrize("name", RECIPES)
def test_every_readme_command_is_run_by_the_clean_runner_ci_job(name):
    """6.4: CI clones the commit under test from the workflow's own repo, so
    the clone line is matched on its shape; every other command must appear
    in the job verbatim."""
    job = _ci_job(RECIPES[name])
    for command in _commands(_recipe(name)):
        if command.startswith("git clone "):
            assert command == "git clone https://github.com/yib7/LeetCoach.git"
            assert "git clone --quiet" in job and " LeetCoach" in job
            continue
        assert command in job, f"README {name} command not run by CI: {command}"


def test_posix_recipe_installs_from_the_committed_lock():
    """6.7: the documented install is constrained by requirements/lock.txt."""
    commands = _commands(_recipe("Linux and macOS"))
    assert any("-c requirements/lock.txt" in c for c in commands), commands
    assert (REPO_ROOT / "requirements" / "lock.txt").is_file()
