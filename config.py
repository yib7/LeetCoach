"""Central config for LeetCoach. Every value is environment-overridable.

These knobs are the only machine-specific settings the app needs:

- ``LEETCOACH_MODEL``       — Claude model id passed to ``claude --model`` (default
                              ``claude-opus-4-8``; override with a smaller/faster
                              alias like ``sonnet`` to save your subscription budget).
- ``LEETCOACH_CLASSIFIER_MODEL`` — model for the short classification call (default
                              ``haiku`` — classifying a problem is trivial, so the
                              cheapest model saves budget on every run).
- ``LEETCOACH_QUICK_ASK_MODEL`` — model for the Quick Ask lookup (default
                              ``haiku`` — a syntax/stdlib question is a trivial
                              lookup, so the cheapest model keeps the feature
                              near-free and spares the subscription budget).
- ``LEETCOACH_CLAUDE_BIN``  — name/path of the ``claude`` executable (default
                              ``claude``; set an absolute path if it is not on PATH).
- ``LEETCOACH_OUTPUT_DIR``  — where the study library is written (default: the
                              ``output`` directory next to this file, so the
                              library never forks when the app is launched from
                              a different working directory; a relative override
                              stays relative — that is the user's explicit choice).
- ``LEETCOACH_RUN_TIMEOUT`` — wall-clock cap in seconds for a single ``claude`` run
                              (default ``600``); a hung CLI is killed after this long.
- ``LEETCOACH_VERIFY_TIMEOUT`` — wall-clock cap in seconds for each Answer-mode
                              sample-verification subprocess (default ``10``); a
                              wedged solution is tree-killed after this long.
- ``LEETCOACH_CLAUDE_CWD``  — neutral working directory every ``claude`` call runs
                              in (A7), so the CLI never picks up this repo's
                              ``CLAUDE.md``/settings and its saved sessions land
                              in their own project bucket (default
                              ``%LOCALAPPDATA%\\LeetCoach\\claude-cwd`` on Windows,
                              ``~/.local/share/leetcoach/claude-cwd`` elsewhere).

Reading env at *call time* (not import time) keeps tests able to monkeypatch the
environment without re-importing the module.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

# Defaults live here so they are documented in one place and referenced by name.
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_CLASSIFIER_MODEL = "haiku"  # classification is trivial; cheapest model wins
DEFAULT_QUICK_ASK_MODEL = "haiku"  # a syntax lookup is trivial; cheapest model wins
DEFAULT_CLAUDE_BIN = "claude"
# Anchored next to this file (audit6 P2-10): a CWD-relative default would let
# `flask run` (or any launch from another directory) silently fork the study
# library and its topic index.
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"
DEFAULT_RUN_TIMEOUT = 600.0  # seconds; generous — Opus study material can be slow
DEFAULT_VERIFY_TIMEOUT = 10.0  # seconds; per sample-verification subprocess

# Upper bound accepted for either timeout knob (B5): 24h is already an absurd
# wait for a local tool. A value past it is CLAMPED to this ceiling (#6) rather
# than silently discarded in favor of the (much shorter) default — a user who
# deliberately asked for a long-running verify/run budget still gets the
# longest sane wait, not a surprise 10s/600s instead. `inf`/NaN/non-positive
# values are still rejected outright and fall back to the default: an infinite
# verify timeout would defeat the whole point of the sandbox's containment
# watchdog, hanging forever instead of tree-killing the child.
MAX_TIMEOUT_SECONDS = 86400.0


def model() -> str:
    """Claude model id used for every `claude --model <id>` call."""
    return os.environ.get("LEETCOACH_MODEL", DEFAULT_MODEL)


# The generic aliases the in-app model picker offers. Aliases (not pinned ids)
# so each always resolves to the CLI's current opus/sonnet/haiku — nothing to
# maintain as new versions ship. Anything reaching `--model` is allowlisted to
# one of these; an explicit id like ``claude-opus-5`` is still settable by hand
# in ``.env``.
ALLOWED_MODEL_ALIASES = ("opus", "sonnet", "haiku")


def model_alias() -> str:
    """The picker alias that best matches the currently configured model.

    Maps the active :func:`model` id to one of :data:`ALLOWED_MODEL_ALIASES` by
    substring (so the default ``claude-opus-4-8`` highlights ``opus``). Returns
    ``""`` when the configured model matches no alias — the picker then shows no
    selection rather than a wrong one.
    """
    current = model().lower()
    for alias in ALLOWED_MODEL_ALIASES:
        if alias in current:
            return alias
    return ""


def classifier_model() -> str:
    """Model id/alias for the classifier's short Claude call (audit6 P2-4).

    Separate from :func:`model` because classification is a trivial task — a
    tiny JSON object naming the technique — so it defaults to the cheapest
    alias (``haiku``) regardless of which model produces the study material.
    """
    return os.environ.get("LEETCOACH_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL)


def quick_ask_model() -> str:
    """Model id/alias for the Quick Ask lookup call.

    Separate from :func:`model` for the same reason as :func:`classifier_model`:
    a Quick Ask is a trivial syntax/stdlib question, so it defaults to the
    cheapest alias (``haiku``) regardless of which model produces the study
    material. Override with ``LEETCOACH_QUICK_ASK_MODEL``.
    """
    return os.environ.get("LEETCOACH_QUICK_ASK_MODEL", DEFAULT_QUICK_ASK_MODEL)


def claude_bin() -> str:
    """Name or absolute path of the `claude` executable."""
    return os.environ.get("LEETCOACH_CLAUDE_BIN", DEFAULT_CLAUDE_BIN)


def _clamped_timeout(env_var: str, default: float) -> float:
    """Shared parsing for both timeout knobs (B5/#6): a float in
    ``(0, MAX_TIMEOUT_SECONDS]``, clamping anything above the ceiling down to
    it, else ``default``.

    Rejects (falls back to ``default``) everything that could defeat a
    wall-clock containment watchdog: an unparseable value, zero, negatives,
    ``NaN`` (``NaN > 0`` is ``False``, catching it in the same comparison), and
    ``inf``/``-inf`` (explicitly checked — ``inf > 0`` is otherwise ``True``).
    A finite value past the 24h ceiling is CLAMPED to the ceiling instead of
    being discarded (#6) — a deliberately long timeout still gets the longest
    sane wait rather than silently reverting to the (much shorter) default.
    """
    raw = os.environ.get(env_var, "")
    try:
        value = float(raw)
    except ValueError:
        return default
    if not value > 0 or math.isinf(value):
        return default
    if value > MAX_TIMEOUT_SECONDS:
        return MAX_TIMEOUT_SECONDS
    return value


def run_timeout() -> float:
    """Wall-clock cap (seconds) for a single `claude` run.

    ``claude_cli``'s watchdog tree-kills the subprocess after this long and the
    run fails with a clear "timed out" error, instead of a hung CLI (network
    stall, stuck auth prompt, wedged node) wedging the Flask worker forever.
    Override with ``LEETCOACH_RUN_TIMEOUT``; invalid, non-positive, or infinite
    values fall back to the default (a broken knob must never disable the
    watchdog or crash a run); an over-ceiling value is clamped to
    :data:`MAX_TIMEOUT_SECONDS` instead of discarded.
    """
    return _clamped_timeout("LEETCOACH_RUN_TIMEOUT", DEFAULT_RUN_TIMEOUT)


def verify_timeout() -> float:
    """Wall-clock cap (seconds) for each Answer-mode sample-verification run.

    The sandbox tree-kills a generated solution after this long so a wedged or
    infinite-looping answer can't hang a run (each parsed sample is bounded
    independently). Override with ``LEETCOACH_VERIFY_TIMEOUT``; invalid,
    non-positive, or infinite values fall back to the default (a broken knob
    must never disable the containment timeout); an over-ceiling value is
    clamped to :data:`MAX_TIMEOUT_SECONDS` instead of discarded.
    """
    return _clamped_timeout("LEETCOACH_VERIFY_TIMEOUT", DEFAULT_VERIFY_TIMEOUT)


def output_dir() -> Path:
    """Root directory of the generated study library (created lazily elsewhere).

    Defaults to the ``output`` directory next to this file — absolute, so the
    library does not depend on the process's CWD. An explicit
    ``LEETCOACH_OUTPUT_DIR`` override is used verbatim (relative stays relative).
    """
    override = os.environ.get("LEETCOACH_OUTPUT_DIR")
    if override:
        return Path(override)
    return DEFAULT_OUTPUT_DIR


def topic_index_path() -> Path:
    """Path to the persisted topic index JSON.

    Defaults to ``<output_dir>/topic_index.json`` (gitignored). Overridable with
    ``LEETCOACH_TOPIC_INDEX`` for tests / alternative locations. Read at call
    time so tests can monkeypatch the environment.
    """
    override = os.environ.get("LEETCOACH_TOPIC_INDEX")
    if override:
        return Path(override)
    return output_dir() / "topic_index.json"


def default_claude_cwd(*, os_name=None, env=None, home=None) -> Path:
    """The platform default for :func:`claude_cwd` (A7).

    Windows: ``%LOCALAPPDATA%\\LeetCoach\\claude-cwd`` (falling back to
    ``~/AppData/Local`` when the variable is missing); elsewhere
    ``~/.local/share/leetcoach/claude-cwd``. The parameters exist only so tests
    can exercise both branches on one machine.
    """
    os_name = os.name if os_name is None else os_name
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    if os_name == "nt":
        base = env.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "LeetCoach" / "claude-cwd"
    return home / ".local" / "share" / "leetcoach" / "claude-cwd"


def claude_cwd() -> Path:
    """Neutral working directory for every ``claude`` subprocess (A7).

    Running ``claude -p`` in the repo made it load this project's
    ``CLAUDE.md``/``.claude`` settings and filed every run under the repo's
    session history. A dedicated directory keeps the CLI's view of the world
    empty and gives LeetCoach's persisted sessions (needed for ``--resume``)
    their own project bucket. Override with ``LEETCOACH_CLAUDE_CWD``; the
    directory is created on demand by ``claude_cli.ensure_claude_cwd``.
    """
    override = os.environ.get("LEETCOACH_CLAUDE_CWD")
    if override:
        return Path(override)
    return default_claude_cwd()


def _env_line_key(line: str) -> str | None:
    """The variable name a ``.env`` line assigns, or ``None`` if it assigns none.

    Blank lines and ``#`` comments assign nothing. A leading ``export`` is
    tolerated. Only a line containing ``=`` is an assignment.
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.startswith("export "):
        s = s[len("export "):].lstrip()
    key, sep, _ = s.partition("=")
    return key.strip() if sep else None


def upsert_env_var(path, key: str, value: str) -> None:
    """Set ``key=value`` in the dotenv file at ``path``, in place.

    Replaces the first existing assignment to ``key`` (preserving every other
    line, comment, and blank), or appends the assignment when the key is absent.
    Creates the file if it does not exist. This is how the in-app model picker
    persists ``LEETCOACH_MODEL`` so the choice survives a restart. Pure I/O on
    the given path — the live process env is updated separately by the caller.
    """
    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    new_line = f"{key}={value}"
    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and _env_line_key(line) == key:
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new_line)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
