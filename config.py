"""Central config for LeetCoach. Every value is environment-overridable.

These knobs are the only machine-specific settings the app needs:

- ``LEETCOACH_MODEL``       — Claude model alias/id passed to ``claude --model``
                              (default ``opus``, an alias the CLI resolves to the
                              latest Opus; override with ``fable`` for the most
                              capable model, or a smaller/faster alias like
                              ``sonnet`` to save your subscription budget).
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

import codecs
import math
import os
from pathlib import Path

import fsutil

# Defaults live here so they are documented in one place and referenced by name.
# An alias (not a pinned id): the CLI resolves it to the newest Opus, so the
# default keeps tracking new releases with nothing to maintain here.
DEFAULT_MODEL = "opus"
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


def _env_str(name: str, default: str) -> str:
    """``os.environ[name]`` stripped, or ``default`` when it is unset, empty
    or blank (3A C10): ``LEETCOACH_MODEL=`` in ``.env`` used to reach argv as
    ``--model ""`` (and an empty binary name could not be run at all)."""
    return (os.environ.get(name) or "").strip() or default


def model() -> str:
    """Claude model id used for every `claude --model <id>` call."""
    return _env_str("LEETCOACH_MODEL", DEFAULT_MODEL)


# The generic aliases the in-app model picker offers. Aliases (not pinned ids)
# so each always resolves to the CLI's current opus/sonnet/haiku — nothing to
# maintain as new versions ship. Anything reaching `--model` is allowlisted to
# one of these; an explicit id like ``claude-opus-5`` is still settable by hand
# in ``.env``.
ALLOWED_MODEL_ALIASES = ("fable", "opus", "sonnet", "haiku")

# The newest model behind each alias, as of the last review. DISPLAY ONLY (the
# picker's button tooltips): the alias, never this id, is what reaches
# ``claude --model``, so the app keeps tracking the CLI's latest models even if
# this table goes stale. Bump it when a new generation ships.
LATEST_MODEL_IDS = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}


def model_label(alias: str) -> str:
    """Human label for an alias from :data:`LATEST_MODEL_IDS`, e.g. ``Opus 5.5``.

    Display only. Falls back to the capitalised alias if it has no entry.
    """
    model_id = LATEST_MODEL_IDS.get(alias)
    if not model_id:
        return alias.capitalize()
    parts = model_id.split("-")  # claude-opus-5-5 -> ["claude", "opus", "5", "5"]
    return f"{parts[1].capitalize()} {'.'.join(parts[2:])}"


def model_alias() -> str:
    """The picker alias that best matches the currently configured model.

    Maps the active :func:`model` id to one of :data:`ALLOWED_MODEL_ALIASES` by
    substring (so both the default ``opus`` alias and a pinned
    ``claude-opus-5-5`` highlight ``opus``). Returns
    ``""`` when the configured model matches no alias — the picker then shows no
    selection rather than a wrong one.
    """
    current = model().lower()
    for alias in ALLOWED_MODEL_ALIASES:
        if alias in current:
            return alias
    return ""


def resolve_run_model(alias: str) -> str:
    """The ``--model`` value for a per-run picker ``alias`` (SP4 review I1).

    When ``alias`` is the picker button the configured model already maps to
    (:func:`model_alias`), the configured value itself is used, so a pinned id
    in ``.env`` (``LEETCOACH_MODEL=claude-sonnet-4-5``) is not silently
    swapped for the generic ``sonnet`` alias just because the page posts the
    highlighted button. Any other alias is used as-is; ``""`` stays ``""``.
    """
    if alias and alias == model_alias():
        return model()
    return alias


def classifier_model() -> str:
    """Model id/alias for the classifier's short Claude call (audit6 P2-4).

    Separate from :func:`model` because classification is a trivial task — a
    tiny JSON object naming the technique — so it defaults to the cheapest
    alias (``haiku``) regardless of which model produces the study material.
    """
    return _env_str("LEETCOACH_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL)


def quick_ask_model() -> str:
    """Model id/alias for the Quick Ask lookup call.

    Separate from :func:`model` for the same reason as :func:`classifier_model`:
    a Quick Ask is a trivial syntax/stdlib question, so it defaults to the
    cheapest alias (``haiku``) regardless of which model produces the study
    material. Override with ``LEETCOACH_QUICK_ASK_MODEL``.
    """
    return _env_str("LEETCOACH_QUICK_ASK_MODEL", DEFAULT_QUICK_ASK_MODEL)


def claude_bin() -> str:
    """Name or absolute path of the `claude` executable."""
    return _env_str("LEETCOACH_CLAUDE_BIN", DEFAULT_CLAUDE_BIN)


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

    The home directory is looked up only on a branch that needs it (3A C5):
    ``Path.home()`` raises ``RuntimeError`` when no home variable is set, and
    a Windows box with ``LOCALAPPDATA`` must not trip over that. It can still
    raise when the home is needed and unresolvable; ``ensure_claude_cwd``
    guards that.
    """
    os_name = os.name if os_name is None else os_name
    env = os.environ if env is None else env

    def _home() -> Path:
        return Path.home() if home is None else Path(home)

    if os_name == "nt":
        base = env.get("LOCALAPPDATA") or str(_home() / "AppData" / "Local")
        return Path(base) / "LeetCoach" / "claude-cwd"
    return _home() / ".local" / "share" / "leetcoach" / "claude-cwd"


def claude_cwd() -> Path:
    """Neutral working directory for every ``claude`` subprocess (A7).

    Running ``claude -p`` in the repo made it load this project's
    ``CLAUDE.md``/``.claude`` settings and filed every run under the repo's
    session history. A dedicated directory keeps the CLI's view of the world
    empty and gives LeetCoach's persisted sessions (needed for ``--resume``)
    their own project bucket. Override with ``LEETCOACH_CLAUDE_CWD``; the
    directory is created on demand by ``claude_cli.ensure_claude_cwd``.

    The override is ``~``-expanded and made absolute (3A C7): ``~/lc`` from
    ``.env`` used to create a literal ``~`` folder under the launch directory,
    often inside this repo, whose ``CLAUDE.md`` the CLI would then load. If
    either step fails (no resolvable home) the value is used as written.
    """
    override = (os.environ.get("LEETCOACH_CLAUDE_CWD") or "").strip()
    if override:
        path = Path(override)
        try:
            return path.expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return path
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


def _quote_closes(text: str, quote: str) -> int:
    """Index just past the closing ``quote`` in ``text`` (which starts right
    after the opening one), or ``-1`` if it does not close on this line. Inside
    double quotes a backslash escapes the next character (python-dotenv's
    rule); single quotes have no escapes."""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return -1


def _env_entries(text: str) -> list[tuple[str | None, list[str]]]:
    """Split dotenv ``text`` into ``(key, lines)`` entries (SP4 review M3).

    Lines are split on ``"\n"`` only (a trailing ``"\r"`` is dropped) -
    ``str.splitlines`` would also break on U+2028, ``\x85``, form feed and
    friends, which are ordinary characters in a value. An assignment whose
    value opens a quote that does not close on the same line swallows the
    following lines up to the closing quote, so text INSIDE a quoted
    multi-line value is never mistaken for an assignment. A quote that never
    closes before EOF swallows nothing: that line is an entry on its own and
    the lines after it are parsed normally. ``key`` is ``None``
    for blanks, comments and anything else that assigns nothing.
    """
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()  # the final newline ends the last line, no extra one
    lines = [ln.removesuffix("\r") for ln in raw_lines]
    entries: list[tuple[str | None, list[str]]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        key = _env_line_key(line)
        group = [line]
        i += 1
        if key is not None:
            value = line.split("=", 1)[1].lstrip()
            if value[:1] in ("'", '"'):
                quote = value[0]
                if _quote_closes(value[1:], quote) < 0:
                    # Look ahead for the closing quote; only a quote that really
                    # closes makes the following lines part of this value. One
                    # that never closes is a malformed single line (python-dotenv
                    # skips it and parses the next lines normally), so nothing
                    # is consumed - otherwise an upsert would replace or drop
                    # the rest of the file along with it.
                    j = i
                    while j < len(lines) and _quote_closes(lines[j], quote) < 0:
                        j += 1
                    if j < len(lines):
                        group.extend(lines[i:j + 1])
                        i = j + 1
        entries.append((key, group))
    return entries


def _decode_env_bytes(data: bytes) -> str:
    """Decode ``.env`` bytes (B12): UTF-8 (with or without a BOM) or UTF-16
    (BOM, or BOM-less LE/BE detected by its NUL pattern - what Windows
    PowerShell 5.1 ``Out-File``/``>`` produce). Raises ``UnicodeDecodeError``
    (a ``ValueError``) for anything else rather than guessing."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    if bytes(1) in data and len(data) % 2 == 0:
        if data[1::2].count(0) * 2 >= len(data) // 2:
            return data.decode("utf-16-le")
        if data[0::2].count(0) * 2 >= len(data) // 2:
            return data.decode("utf-16-be")
    return data.decode("utf-8")


def read_env_text(path) -> str:
    """The text of the dotenv file at ``path``, decoded per
    :func:`_decode_env_bytes`; ``""`` when it does not exist. Any other
    ``OSError`` and any decode error propagate - callers must not mistake an
    unreadable file for an empty one (B12: that used to wipe ``.env``)."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return ""
    return _decode_env_bytes(data)


def upsert_env_var(path, key: str, value: str) -> None:
    """Set ``key=value`` in the dotenv file at ``path``, in place.

    Updates the FIRST assignment to ``key`` in place and removes every later
    one (dotenv is last-wins, so a stale duplicate would otherwise override
    the choice - SP4 review M4), preserving every other line, comment, and
    blank, or appends the assignment when the key is absent. A ``key=`` that
    sits inside another key's quoted multi-line value is not an assignment and
    is left alone (M3). Creates the file if it does not exist. This is how the
    in-app model picker persists ``LEETCOACH_MODEL`` so the choice survives a
    restart.

    B12 robustness: a UTF-8 BOM or UTF-16 file is read correctly and written
    back as plain UTF-8 (what python-dotenv reads at boot); a read or decode
    error ABORTS (raises ``OSError`` / ``ValueError``) instead of treating the
    file as empty and wiping it; the write is atomic (:mod:`fsutil`). Pure I/O
    on the given path - the live process env is updated by the caller.
    """
    new_line = f"{key}={value}"
    out: list[str] = []
    replaced = False
    for entry_key, group in _env_entries(read_env_text(path)):
        if entry_key != key:
            out.extend(group)
        elif not replaced:
            out.append(new_line)
            replaced = True
        # else: a later duplicate - dropped, with any continuation lines
    if not replaced:
        out.append(new_line)
    fsutil.atomic_write_text(path, "\n".join(out) + "\n")
