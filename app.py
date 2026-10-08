"""Flask web layer for LeetCoach: a single page, an SSE `/run` endpoint, the
read-only `/library` pair, and the plain-JSON Quick Ask `/ask` endpoint.

Design mirrors the Xeno RAG pattern, in Flask flavour:

* ``create_app(*, run_fn=claude_cli.run)`` is a factory with an **injectable**
  Claude runner. The SAME ``run_fn`` is threaded into BOTH the classifier and
  the answer stream, so a single injected fake (tests) covers every Claude call
  while the real orchestration + save still runs end-to-end.
* ``/run`` returns ``Response(stream_with_context(event_stream()),
  mimetype="text/event-stream")``. The generator yields ``data:`` text events
  for each delta and a terminal ``event: done`` (or ``event: error``) so the
  stream always closes cleanly — a last-resort ``except`` guarantees it.

All three modes (Answer / Learning / Guided) are wired here. They share one
shape — classify -> build a mode-specific prompt -> stream + accumulate the
deltas -> save the result -> emit a terminal ``done`` — so the streaming and the
``done``/``error`` plumbing live in one place (``event_stream`` +
``_stream_and_accumulate``); only the per-mode prompt-builder and save call
differ.

SSE event protocol (shared by every mode):
    data: "<text delta>"\n\n                 # incremental answer text (json string)
    event: done\ndata: {json}\n\n             # terminal success:
        { "problem_type": str, "topics": [str], "paths": [str], "mode": str,
          "verification": str (Answer/Guided only — the sandbox verdict line) }
    event: error\ndata: "<message>"\n\n        # terminal failure (json string)
"""
from __future__ import annotations

import io
import json
import os
import queue
import re
import socket
import threading
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context

import classifier
import claude_cli
import config
import parsing
import prompts
import sandbox
import storage
import topic_index


def _maybe_load_dotenv(path: Path) -> None:
    """Load ``path`` as a dotenv file, unless ``LEETCOACH_NO_DOTENV`` is set
    (B8). Never overrides vars already set in the real environment; a missing
    ``.env`` is a silent no-op either way. Split out as its own function (not
    inlined at import time) so it's directly unit-testable without reloading
    this module or touching the real project ``.env``.
    """
    if os.environ.get("LEETCOACH_NO_DOTENV", "").strip().lower() in {"1", "true", "yes"}:
        return
    # B12: decode it ourselves (UTF-8 with/without BOM, UTF-16 as written by
    # Windows PowerShell) - python-dotenv assumes plain UTF-8, so a UTF-16
    # .env crashed the boot and a BOM corrupted the first key. A file that
    # still can't be read is skipped with a warning, never a crash.
    try:
        text = config.read_env_text(path)
    except (OSError, ValueError) as exc:
        print(f"WARNING: could not read {path} ({exc}); ignoring it.")
        return
    if text:
        load_dotenv(stream=io.StringIO(text))


# Load .env from the project root (next to this file) if present, so LEETCOACH_*
# settings written to a .env take effect for `python app.py` and WSGI imports.
# The test suite sets LEETCOACH_NO_DOTENV=1 (tests/conftest.py) so importing
# `app` never reads the developer's real .env or leaks its settings into the
# test process.
_maybe_load_dotenv(Path(__file__).resolve().parent / ".env")

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

# Bind address for `python app.py` (single source of truth — the Host-header
# allowlist below keys off loopback hostnames, so no port is duplicated here).
HOST = "127.0.0.1"
PORT = 5000

# Reported by GET /healthz (D16) so a second launch can recognise a running
# LeetCoach. Matches the CHANGELOG's current release line.
VERSION = "1.4.0"

# Host-header allowlist (DNS-rebinding defense). Hostnames only, ANY port: a
# rebinding attacker controls what IP their hostname resolves to, never the
# hostname the victim's browser sends — so matching the hostname IS the whole
# defense, and pinning a port would only break `flask run` on a non-default
# port. Bracketed "[::1]" covers the IPv6 loopback literal.
ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})

# Cap on how many already-learned topics get interpolated into the Learning
# prompt (audit6 P2-12): the index grows forever, the prompt must not. The
# stored list is insertion-ordered, so "the most recent N" is its tail.
LEARNED_TOPICS_CAP = 50

# A6: the longest a finished answer waits on the background classifier before
# saving under the fallback type (and cancelling the classifier call). The
# classifier normally answers in a few seconds on the cheap model.
CLASSIFIER_JOIN_TIMEOUT = 60.0

# Extensions the library browser (SP10) will list and serve. Everything the
# app itself writes (storage._LANG_EXT + .md, .txt fallback, topic_index.json)
# is covered; anything else in the output dir is invisible to the read path.
LIBRARY_EXTENSIONS = frozenset({".md", ".py", ".cpp", ".java", ".txt", ".json"})

# B10: the longest a cached /library listing is served without a re-walk, even
# when no directory mtime moved (an in-place edit of a file). Read at call time.
LIBRARY_CACHE_TTL = 30.0

# B19: the sibling extensions that make up one saved run (an Answer's notes +
# code). ``DELETE /library/file?scope=run`` removes all of them together.
RUN_SIBLING_EXTENSIONS = (".md", ".py", ".cpp", ".java", ".txt")

# Quick Ask bounds: the question stays small (it's a syntax lookup, not an
# essay), and the optional problem CONTEXT is capped server-side so a pasted
# novel can't balloon the Haiku prompt.
QUICK_ASK_MAX_QUESTION = 500
QUICK_ASK_PROBLEM_CONTEXT_CAP = 6000

# Allowlists — never pass an arbitrary string downstream to prompts/storage.
MODES = ("answer", "learning", "guided")
LANGUAGES = prompts.LANGUAGES          # ("python", "cpp", "java")
TIERS = prompts.TIERS                  # ("simple", "normal", "complex")


def _json_object() -> tuple[dict, Response | None]:
    """Parse the request body as JSON and return ``(data, None)``, or
    ``(_, error_response)`` if the body isn't a JSON OBJECT (B23).

    ``request.get_json(silent=True)`` happily returns a list/string/number for
    a JSON array/string/number body, and every route below immediately calls
    ``.get()`` on the result — an ``AttributeError`` -> bare 500 for any
    script/curl that posts a non-object body. A missing/empty body still
    parses to ``None`` and is treated as ``{}`` (unchanged: every field is then
    "missing", handled by each route's own validation) since it's
    indistinguishable from an explicit JSON ``null``.
    """
    data = request.get_json(silent=True)
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, (jsonify({"error": "Request body must be a JSON object."}), 400)
    return data, None


def _non_string_field_error(data: dict, fields) -> str | None:
    """Return a 400-worthy message if any named field is PRESENT but not a
    string, else ``None``. ``fields`` is an iterable of ``(key, Label)`` pairs.

    These endpoints are unauthenticated local routes any script/curl can hit,
    so a JSON number/list/object in a text field must yield a clean 400 — not an
    ``AttributeError``/``TypeError`` 500 from a downstream ``.strip()`` or slice
    (checklist 3.12). A missing field (``None``) and a real string both pass, so
    the existing missing/string handling downstream is untouched."""
    for key, label in fields:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            return f"{label} must be text."
    return None


def _hostname(host: str) -> str:
    """The hostname part of a Host header value, port stripped, lowercased.
    Handles the bracketed IPv6 form ("[::1]:5000" -> "[::1]")."""
    host = host.strip().lower()
    if host.startswith("["):
        return host.partition("]")[0] + "]"
    return host.rsplit(":", 1)[0]


_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _split_host_port(hostport: str, scheme: str) -> tuple[str, int | None]:
    """``("host", port)`` from a ``host[:port]`` string (bracketed IPv6 ok),
    the scheme's default port filled in; port ``None`` if unparseable."""
    hostport = hostport.strip().lower()
    host = _hostname(hostport)
    rest = hostport[len(host):]
    if not rest:
        return host, _DEFAULT_PORTS.get(scheme)
    if not rest.startswith(":") or not rest[1:].isdigit():
        return host, None
    return host, int(rest[1:])


def _same_origin(origin: str, host_header: str, scheme: str) -> bool:
    """C2: True iff the ``Origin`` header names exactly this server - same
    scheme, same loopback host, same port as the request's ``Host``. ``null``
    (sandboxed iframes, file://) and anything unparseable are rejected."""
    parsed = urlsplit(origin.strip())
    if parsed.scheme not in _DEFAULT_PORTS or not parsed.netloc:
        return False
    if parsed.scheme != scheme or parsed.path not in ("", "/"):
        return False
    origin_host = _split_host_port(parsed.netloc, parsed.scheme)
    return origin_host[1] is not None and origin_host == _split_host_port(host_header, scheme)


def _cross_site_rejection(method: str, path: str, headers, host_header: str, scheme: str):
    """C2: the reason to refuse a request as cross-site, or ``None``.

    Unsafe methods (every state-changing route: /run, /ask, /config/model,
    DELETE /library/file, /run/cancel) must come from this page: a browser
    sends ``Origin`` on them, and it must be this exact origin;
    ``Sec-Fetch-Site: cross-site`` is refused outright. A request with neither
    header (curl, scripts, the test client) is allowed - the threat is a
    hostile web page, which cannot strip them.

    ``GET /`` runs the CLI sign-in probe, so a cross-site page must not be able
    to trigger it with an ``<img>``/``<iframe>``/``fetch``; only a top-level
    navigation (the user following a link) is allowed cross-site.
    """
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if method in _UNSAFE_METHODS:
        if site == "cross-site":
            return "Cross-site request refused."
        origin = headers.get("Origin")
        if origin is not None and not _same_origin(origin, host_header, scheme):
            return "Cross-origin request refused."
        return None
    if path == "/" and site == "cross-site":
        mode = (headers.get("Sec-Fetch-Mode") or "").strip().lower()
        dest = (headers.get("Sec-Fetch-Dest") or "").strip().lower()
        if not (mode == "navigate" and dest == "document"):
            return "Cross-site request refused."
    return None


# C3: an SSE comment frame sent while the stream is otherwise silent (Claude
# thinking, the sandbox verifying, the classifier join). It keeps proxies and
# the browser from timing the stream out, and - the B14 point - a write to a
# client that has gone away fails, so the server notices the disconnect and
# frees the run instead of waiting for Claude to finish. Ignored by EventSource
# and by app.js's parser. The interval is read at call time (tests shorten it).
SSE_PING = ": ping\n\n"
SSE_PING_INTERVAL = 15.0

_HEARTBEAT = object()
_ITEM, _DONE, _ERROR = "item", "done", "error"


def _iter_with_heartbeat(iterable):
    """Yield the items of ``iterable`` - pulled on a helper thread - and
    ``_HEARTBEAT`` whenever ``SSE_PING_INTERVAL`` passes with no new item.
    Exceptions from the iterable re-raise here. If the consumer stops early
    (client disconnect, cancel), ``iterable.cancel()`` is called when it has
    one (``claude_cli.ClaudeRun`` kills its process tree) and the helper
    thread closes the iterator as soon as it regains control."""
    q: queue.Queue = queue.Queue()
    stop = threading.Event()

    def pump():
        it = iter(iterable)
        try:
            for item in it:
                q.put((_ITEM, item))
                if stop.is_set():
                    break
        except BaseException as exc:  # noqa: BLE001 - handed to the consumer
            q.put((_ERROR, exc))
            return
        finally:
            if stop.is_set():
                close = getattr(it, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - best-effort teardown
                        pass
        q.put((_DONE, None))

    threading.Thread(target=pump, name="leetcoach-stream-pump", daemon=True).start()
    finished = False
    try:
        while True:
            try:
                kind, value = q.get(timeout=SSE_PING_INTERVAL)
            except queue.Empty:
                yield _HEARTBEAT
                continue
            if kind is _DONE:
                finished = True
                return
            if kind is _ERROR:
                finished = True
                raise value
            yield value
    finally:
        if not finished:
            stop.set()
            cancel = getattr(iterable, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass


def _call_with_heartbeat(fn):
    """Run the blocking ``fn()`` on a helper thread, yielding ``SSE_PING``
    every ``SSE_PING_INTERVAL`` while it works; ``return`` its result (use
    with ``yield from`` inside an SSE generator) or re-raise its exception."""
    box: dict = {}
    done = threading.Event()

    def work():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=work, name="leetcoach-blocking-call", daemon=True).start()
    while not done.wait(SSE_PING_INTERVAL):
        yield SSE_PING
    if "error" in box:
        raise box["error"]
    return box["value"]


class _RunCancelled(Exception):
    """Raised inside a /run stream once ``POST /run/cancel`` named it."""


class _RunState:
    """One in-flight /run (B14): its dedup key and the cancel hooks of every
    Claude call it started, so ``POST /run/cancel`` can kill them all."""

    def __init__(self, key) -> None:
        self.key = key
        self._lock = threading.Lock()
        self._cancelled = False
        self._hooks: list = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def add_cancel_hook(self, hook) -> None:
        with self._lock:
            self._hooks.append(hook)
            fire = self._cancelled
        if fire:
            hook()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            hooks = list(self._hooks)
        for hook in hooks:
            hook()

    def check(self) -> None:
        if self._cancelled:
            raise _RunCancelled()


_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _sweep_sandbox_temp() -> int:
    """Startup housekeeping (SP3 B6): remove ``leetcoach_run_*`` sandbox dirs
    older than a day that a crashed or killed run left in the temp dir.
    Returns how many were removed; never raises, so a hiccup can't block
    launch."""
    try:
        return sandbox.sweep_stale_run_dirs()
    except Exception:  # noqa: BLE001 - housekeeping must never stop the app launching
        return 0


def _choose_port(preferred: int, host: str, *, span: int = 20) -> int:
    """Pick a bindable TCP port on ``host``, preferring ``preferred`` (SP-A).

    Probe-bind a fresh ``SOCK_STREAM`` socket (address family derived from ``host``,
    so an IPv6 ``HOST`` such as ``::1`` works as well as IPv4) with NO
    ``SO_REUSEADDR`` (so probing an in-use port genuinely fails); if ``preferred``
    binds, return it. Otherwise scan ``preferred+1 … preferred+span`` (clamped to the
    valid ``<= 65535`` range) and return the first port that binds. If the whole span
    is busy, bind to port ``0`` and return the OS-assigned ephemeral port. Every probe
    socket is closed before returning. A tiny TOCTOU window between the probe and
    ``app.run`` is acceptable for a single-user localhost tool — this only turns a
    hard crash on an occupied port into a graceful fallback."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET

    def _binds(port: int) -> bool:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    # Clamp the scan so a candidate never exceeds the valid port range (a high
    # PORT would otherwise raise OverflowError, not OSError, past 65535).
    for candidate in range(preferred, min(preferred + span, 65535) + 1):
        if _binds(candidate):
            return candidate
    # Whole span occupied (or preferred out of range) — OS-assigned ephemeral port.
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _sse_text(delta: str) -> str:
    """A streamed text delta. JSON-encoded so newlines/markdown survive transport
    (a raw newline is an SSE event boundary)."""
    return f"data: {json.dumps(delta)}\n\n"


def _sse_event(name: str, payload) -> str:
    """A named terminal event carrying a JSON payload."""
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n"


def _verification_line(result) -> str:
    """A short one-line human verdict for the stream + saved markdown, derived
    from a ``sandbox.VerifyResult``."""
    status = getattr(result, "status", "not_verified")
    note = getattr(result, "note", "") or ""
    if status == "pass":
        return f"✓ Sample tests PASS ({note})" if note else "✓ Sample tests PASS"
    if status == "fail":
        return f"✗ Sample tests FAIL ({note})" if note else "✗ Sample tests FAIL"
    if status == "error":
        return f"✗ Sample tests ERROR ({note})" if note else "✗ Sample tests ERROR"
    # not_verified
    return f"⚠ not auto-verified ({note})" if note else "⚠ not auto-verified"


def _verify_code(code: str, problem: str, language: str):
    """Best-effort sandbox verification of pre-extracted ``code`` (the caller
    extracts exactly once — audit6 P2-13). Returns ``(result, verdict_line)``;
    never raises (a verifier hiccup must not break a run). ``result`` may be
    ``None`` if verification couldn't even start."""
    try:
        result = sandbox.verify_answer(code, problem, language)
        return result, _verification_line(result)
    except Exception as exc:  # noqa: BLE001 - verification is strictly best-effort
        return None, f"⚠ not auto-verified (verifier error: {exc})"


def _is_hidden(root: Path, path: Path) -> bool:
    """B19: True for app metadata that is never part of the library - any
    path with a dot-prefixed segment (``.leetcoach/``, temp files), the topic
    index (``topic_index.json`` or wherever ``LEETCOACH_TOPIC_INDEX`` points
    inside the output dir). ``path`` is resolved and inside ``root``."""
    rel = path.relative_to(root)
    if any(part.startswith(".") for part in rel.parts):
        return True
    if rel.as_posix() == "topic_index.json":
        return True
    try:
        return path == config.topic_index_path().resolve()
    except OSError:
        return False


def _library_signature(root: Path) -> tuple:
    """B10: a cheap freshness signature for the listing - the mtime of the
    root AND of every (non-hidden) directory under it. Adding, removing or
    renaming a file anywhere bumps its parent directory's mtime, so a nested
    change made outside the app (Explorer, git, a sync client) invalidates the
    cache too, not just a top-level one."""
    sig = []
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            sig.append((str(folder), folder.stat().st_mtime_ns))
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except OSError:
            sig.append((str(folder), None))
    return tuple(sorted(sig, key=lambda item: item[0]))


def _library_files(root: Path) -> list[dict]:
    """The library listing: every allowlisted file under ``root``, as
    ``{"path": <relative, forward slashes>, "size": <bytes>, "mtime": <epoch
    seconds>}`` dicts, sorted by path. ``mtime`` lets the frontend show real
    saved dates and derive the recent-runs list; it is an additive field, so
    older callers that read only ``path``/``size`` are unaffected. A
    missing/empty root is an empty list, never an error. App metadata
    (``.leetcoach/``, the topic index) is left out (B19)."""
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in LIBRARY_EXTENSIONS:
            continue
        if _is_hidden(root, path):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue  # vanished mid-walk — skip, never 500 a listing
        files.append({
            "path": path.relative_to(root).as_posix(),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    return files


def _resolve_library_file(rel: str) -> Path | None:
    """Resolve a request's ``path`` param against the output root, or ``None``.

    This is the containment gate for the library's read path — the same rigor
    as ``storage.slug`` on the write path. ``None`` (-> a uniform 404) unless
    ALL of the following hold, so a rejection never leaks what exists:

    * non-empty, no NUL, and not absolute / drive-anchored (``C:\\...``,
      ``\\\\server\\share``, ``/etc``, ``\\foo``);
    * ``(root / rel).resolve()`` — which collapses ``..`` (in slash OR
      backslash form; Windows Path treats both as separators) and follows
      symlinks — stays inside ``root.resolve()`` per ``is_relative_to``;
    * the suffix is on ``LIBRARY_EXTENSIONS`` (checked on the RESOLVED path);
    * it is an existing regular file (directories and reserved names fail).
    """
    if not rel or "\x00" in rel:
        return None
    root = config.output_dir().resolve()
    try:
        candidate = Path(rel)
        if candidate.is_absolute() or candidate.drive:
            return None
        resolved = (root / candidate).resolve()
    except (OSError, ValueError):
        return None
    if not resolved.is_relative_to(root):
        return None
    if resolved.suffix.lower() not in LIBRARY_EXTENSIONS:
        return None
    if not resolved.is_file():
        return None
    if _is_hidden(root, resolved):  # B19: app metadata is not a library file
        return None
    return resolved


def _verification_detail(result) -> str:
    """A compact markdown block describing each NON-passing sample (audit6
    P2-9), appended to the SAVED reasoning ``.md`` only — the stream keeps the
    one-line verdict (which already carries the pass/fail counts).

    Empty string unless ``result`` is a fail/error with per-sample detail.
    Input/expected/got/stderr are rendered in fenced blocks so multi-line
    sample bodies (P1-1) stay readable; the sandbox has already truncated and
    capped every captured field.
    """
    if result is None or getattr(result, "status", None) not in ("fail", "error"):
        return ""
    blocks = []
    for entry in getattr(result, "detail", None) or []:
        status = entry.get("status", result.status)
        if status == "pass":
            continue  # the verdict line's counts already cover passing samples
        header = f"**Sample {entry.get('sample', '?')} — {status}**"
        rc = entry.get("returncode")
        if rc is not None:
            header = header[:-2] + f" (exit code {rc})**"
        lines = [header, ""]
        # B4: the verifier's own reason (e.g. "timed out after 10s") — some
        # error paths (timeout, couldn't launch) have a note but no captured
        # stdout at all, and it used to be dropped entirely, leaving the user
        # with "errored 1/1" and no explanation.
        note = str(entry.get("note") or "").strip()
        if note:
            lines += [note, ""]
        for label, key in (("Input", "stdin"), ("Expected", "expected"), ("Got", "stdout")):
            value = str(entry.get(key, "")).rstrip("\n")
            lines += [f"{label}:", "```", value, "```"]
        stderr = str(entry.get("stderr") or "").rstrip("\n")
        if stderr:
            lines += ["Stderr:", "```", stderr, "```"]
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "\n**Failed samples:**\n\n" + "\n\n".join(blocks) + "\n"


def create_app(*, run_fn=claude_cli.run, auth_probe=claude_cli.cached_auth_status) -> Flask:
    """Build the Flask app.

    ``run_fn`` is the injectable Claude runner used by BOTH the classifier and
    the answer stream (tests pass a fake). ``auth_probe`` is the injectable
    sign-in probe the page uses to decide which (if any) CLI banner to show —
    injectable so tests never spawn the real ``claude auth status``. The
    default is the cached probe (B1): ``GET /`` no longer runs the CLI
    synchronously on every page load.
    """
    app = Flask(__name__, template_folder=str(TEMPLATES), static_folder=str(STATIC))

    # Reject oversized request bodies with a clean 413 instead of buffering an
    # unbounded POST into memory (P2-3). A LeetCode problem is a few KB; 2 MiB is
    # generous headroom while still capping a hostile/accidental flood.
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

    # Where the model picker persists its choice. A config value (not a bare
    # constant) so tests can redirect it to a temp file instead of the real
    # project `.env`. #7: honours LEETCOACH_DOTENV_PATH (set by the test
    # suite's autouse fixture, tests/conftest.py) so EVERY create_app() call
    # across the whole suite is redirected away from the real `.env` by
    # default, not just the couple of tests that override app.config by hand.
    app.config["DOTENV_PATH"] = os.environ.get(
        "LEETCOACH_DOTENV_PATH", str(HERE / ".env")
    )

    # In-flight /run de-duplication (P2-12): a single-user local tool should not
    # fan the same (problem, mode, language, tier, model) into N concurrent
    # Claude runs (double-click, impatient re-submit). ``_inflight_runs`` maps
    # each key to the run_id that owns it, ``_runs`` maps run_id -> _RunState;
    # both guarded by one lock. The key is registered after validation and
    # released when the stream ends — on normal completion, client disconnect
    # (GeneratorExit), or error — or right away by POST /run/cancel (B14), and
    # only ever by its owner, so a finishing old run can't free a new one's key.
    _inflight_runs: dict = {}
    _runs: dict = {}
    _inflight_lock = threading.Lock()

    def _release_run(run_id: str, state: _RunState) -> None:
        with _inflight_lock:
            if _inflight_runs.get(state.key) == run_id:
                del _inflight_runs[state.key]
            if _runs.get(run_id) is state:
                del _runs[run_id]

    # Library-listing cache (P2-6): the read-only /library walk (rglob + a stat
    # per file) reran on every tab-open and post-run refresh. Cache the result
    # behind a cheap freshness signature — an app-owned version counter (bumped
    # whenever the app itself saves or deletes a library file) combined with the
    # mtime of every directory in the tree (B10: catches a nested change made
    # outside the app, not just a top-level one) — plus a short TTL so an
    # in-place edit (which changes no directory mtime) still shows up.
    _lib_cache: dict = {"sig": None, "files": None, "at": 0.0}
    _lib_version = [0]
    _lib_lock = threading.Lock()

    def _invalidate_library_cache():
        with _lib_lock:
            _lib_version[0] += 1

    def _cached_library_files() -> list[dict]:
        root = config.output_dir().resolve()
        dirs = _library_signature(root) if root.is_dir() else ()
        with _lib_lock:
            sig = (_lib_version[0], dirs)
            now = time.monotonic()
            if (
                _lib_cache["files"] is None
                or _lib_cache["sig"] != sig
                or now - _lib_cache["at"] >= LIBRARY_CACHE_TTL
            ):
                _lib_cache["files"] = _library_files(root)
                _lib_cache["sig"] = sig
                _lib_cache["at"] = now
            return _lib_cache["files"]

    @app.before_request
    def _reject_foreign_hosts():
        # DNS-rebinding defense (audit P1-3): a malicious page can point its own
        # hostname at 127.0.0.1 and drive /run (spending subscription budget and
        # executing generated code in the sandbox). The browser still sends the
        # attacker's hostname in Host, so rejecting non-loopback hostnames
        # blocks the attack for every route.
        if _hostname(request.host) not in ALLOWED_HOSTNAMES:
            return jsonify({"error": "Forbidden host."}), 403
        # C2: a hostile page on another origin can still POST to loopback
        # (the Host check can't see that) - refuse it by Origin/Sec-Fetch-Site.
        reason = _cross_site_rejection(
            request.method, request.path, request.headers, request.host, request.scheme
        )
        if reason:
            return jsonify({"error": reason}), 403

    @app.after_request
    def _response_headers(resp):
        # Force revalidation of the frontend code so an edited app.js/style.css
        # is never silently served stale during local iteration.
        if request.path == "/" or request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        # Defense-in-depth for the untrusted-markdown surface (Claude's output,
        # incl. the cheaper Quick Ask model, is rendered into the page). Every
        # script/style/font is a self-hosted file and the only images the
        # renderer emits are inline data: URIs, so a strict policy holds without
        # 'unsafe-inline'. connect-src 'self' keeps /run, /ask, /library fetches
        # same-origin. nosniff protects the text/plain /library/file route.
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        # C1: no other page may frame LeetCoach (clickjacking of Run, Delete
        # and the model picker). X-Frame-Options covers older browsers.
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @app.get("/")
    def index():
        # The auth state is probed at request time (not import) so the app
        # constructs without a live claude (tests / CI). The page surfaces the
        # state — installed? signed in? which model? — and never crashes here.
        try:
            status = auth_probe()
            installed, logged_in = status.installed, status.logged_in
        except Exception:  # noqa: BLE001 - the probe must never 500 the page
            installed, logged_in = False, False
        html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        # Inject tiny flags the page reads (kept out of a template engine so the
        # page stays a plain static file editable by hand).
        html = html.replace("__CLAUDE_AVAILABLE__", "true" if installed else "false")
        html = html.replace("__CLAUDE_LOGGED_IN__", "true" if logged_in else "false")
        html = html.replace("__CLAUDE_MODEL__", config.model_alias())
        # Display-only version labels for the picker tooltips (the alias, not
        # this label, is what reaches `--model`).
        for alias in config.ALLOWED_MODEL_ALIASES:
            html = html.replace(
                f"__MODEL_LABEL_{alias.upper()}__", config.model_label(alias)
            )
        return Response(html, mimetype="text/html")

    @app.get("/healthz")
    def healthz():
        # D16: lets a second launch recognise a running LeetCoach (and not
        # some other app) on the preferred port. No probe, no filesystem.
        return jsonify({"app": "leetcoach", "version": VERSION})

    @app.post("/config/model")
    def config_model():
        # Persist the in-app model picker's choice as the default. The alias is
        # allowlisted (it becomes a `--model` argv token) and written to `.env`
        # so it survives a restart; os.environ is updated so the very next run
        # uses it with no restart (config.model() reads env at call time).
        data, err = _json_object()
        if err:
            return err
        alias = data.get("model")
        if alias not in config.ALLOWED_MODEL_ALIASES:
            return jsonify({"error": "Unknown model."}), 400
        try:
            config.upsert_env_var(app.config["DOTENV_PATH"], "LEETCOACH_MODEL", alias)
        except (OSError, ValueError):
            # B12: an unreadable/undecodable .env aborts the upsert (the file
            # is left untouched) instead of being wiped.
            app.logger.exception("could not persist the model choice")
            return jsonify({"error": "Could not save the model setting."}), 500
        os.environ["LEETCOACH_MODEL"] = alias
        return jsonify({"ok": True, "model": alias})

    @app.get("/library")
    def library():
        # Read-only listing of the study library (SP10). Missing/empty output
        # dir is an empty listing — the library just hasn't accumulated yet.
        # Served from the freshness-keyed cache (P2-6).
        return jsonify({"files": _cached_library_files()})

    @app.get("/library/file")
    def library_file():
        # One library file's raw text. Served as text/plain (never HTML) so
        # nothing in the library can execute in the browser; rendering happens
        # client-side through the same hardened pipeline as run output. Every
        # rejection is the same 404 — don't leak which check failed or what
        # exists outside the root.
        resolved = _resolve_library_file(request.args.get("path", ""))
        if resolved is None:
            return jsonify({"error": "Not found."}), 404
        try:
            body = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return jsonify({"error": "Not found."}), 404
        return Response(body, mimetype="text/plain")

    @app.delete("/library/file")
    def delete_library_file():
        # Delete ONE library file (P2-11). Reuses the exact containment gate as
        # the read path, so a traversal / escape / absolute path / non-library
        # suffix / missing file is the SAME uniform 404 — a rejection never
        # leaks whether a target exists or where the root sits, matching the
        # GET /library/file no-leak design. There is deliberately no mass-delete.
        #
        # B19: ``scope=run`` deletes the whole saved run - the file plus its
        # same-stem siblings in the same folder (an Answer's .md AND its code
        # file), so a delete never orphans a .py. A different slot
        # (``<stem>__2``) or tier is a different run and is left alone.
        scope = request.args.get("scope", "file")
        if scope not in ("file", "run"):
            return jsonify({"error": "Unknown delete scope."}), 400
        resolved = _resolve_library_file(request.args.get("path", ""))
        if resolved is None:
            return jsonify({"error": "Not found."}), 404
        targets = [resolved]
        if scope == "run":
            for ext in RUN_SIBLING_EXTENSIONS:
                sibling = resolved.with_suffix(ext)
                if sibling == resolved:
                    continue
                rel_sibling = sibling.relative_to(config.output_dir().resolve()).as_posix()
                if _resolve_library_file(rel_sibling) is not None:
                    targets.append(sibling)
        root = config.output_dir().resolve()
        deleted = []
        for target in targets:
            try:
                target.unlink()
            except OSError:
                # Vanished between the resolve and the unlink, or a
                # permission/lock issue — never 500 the caller.
                continue
            deleted.append(target.relative_to(root).as_posix())
        _invalidate_library_cache()  # next /library reflects the removal
        if not deleted:
            return jsonify({"error": "Not found."}), 404
        return jsonify({"deleted": True, "paths": deleted})

    @app.post("/run")
    def run():
        data, err = _json_object()
        if err:
            return err

        # Type-check before any .strip()/.lower(): a non-string field (a script
        # posting a JSON number/list) must be a clean 400, not a 500 (3.12).
        type_err = _non_string_field_error(
            data,
            (("problem", "Problem"), ("mode", "Mode"),
             ("language", "Language"), ("tier", "Tier"), ("model", "Model"),
             ("run_id", "Run id")),
        )
        if type_err:
            return jsonify({"error": type_err}), 400

        problem = (data.get("problem") or "").strip()
        mode = (data.get("mode") or "").strip().lower()
        language = (data.get("language") or "").strip().lower()
        tier = (data.get("tier") or "").strip().lower()
        # B12: an optional per-run model (the picker sends it with every run,
        # so two tabs no longer share one global choice). Allowlisted because
        # it becomes a `--model` argv token; absent/empty -> config default.
        model = (data.get("model") or "").strip().lower()
        if model and model not in config.ALLOWED_MODEL_ALIASES:
            return jsonify({"error": f"Unknown model {model!r}."}), 400
        # B14: the client names its run so Stop can POST /run/cancel; a
        # client that doesn't gets a server-made id (echoed in X-Run-Id).
        run_id = data.get("run_id")
        if run_id is None:
            run_id = uuid.uuid4().hex
        elif not _RUN_ID_RE.fullmatch(run_id):
            return jsonify({"error": "Invalid run id."}), 400

        # --- validation (reject unknown values up front, before any Claude call)
        if not problem:
            return jsonify({"error": "Problem text is required."}), 400
        if mode not in MODES:
            return jsonify({"error": f"Unknown mode {mode!r}."}), 400
        if language not in LANGUAGES:
            return jsonify({"error": f"Unknown language {language!r}."}), 400
        # Learning has no tier; Answer/Guided require a valid one.
        if mode != "learning" and tier not in TIERS:
            return jsonify({"error": f"Unknown tier {tier!r}."}), 400

        # De-dup identical in-flight runs (P2-12). Register atomically after
        # validation; an exact duplicate that's still streaming gets a 409.
        run_key = (problem, mode, language, tier, model)
        study_kwargs = {"model": model} if model else {}
        state = _RunState(run_key)
        with _inflight_lock:
            if run_key in _inflight_runs or run_id in _runs:
                return jsonify(
                    {"error": "An identical run is already in progress."}
                ), 409
            _inflight_runs[run_key] = run_id
            _runs[run_id] = state

        def _stream_and_accumulate(prompt):
            """Stream ``run_fn(prompt)`` deltas to the client (yielding SSE text
            events) while accumulating the full text. Returns the joined text via
            a one-element list trick — generators can't ``return`` a value the
            caller easily reads while also yielding, so we stash it on ``out[0]``.

            If iterating ``run_fn`` fails mid-stream (e.g. the `claude` subprocess
            dies part-way through a response), the exception is caught HERE and
            re-raised only after the accumulator is left in a defined state — so
            the outer ``event_stream`` handler converts it into a terminal SSE
            ``error`` event instead of the stream cutting off silently. Whatever
            text arrived before the failure has already been yielded to the
            client; we do NOT proceed to save a partial/incomplete answer.
            """
            out[0] = ""  # reset accumulator for this call
            full = []
            # A7: study runs carry the tutor persona and KEEP their session
            # (in the neutral cwd's project bucket) for a later --resume.
            try:
                state.check()  # cancelled before the stream even started
                call = run_fn(
                    prompt,
                    system_prompt=prompts.TUTOR_SYSTEM_PROMPT,
                    persist_session=True,
                    **study_kwargs,
                )
                # B14: POST /run/cancel kills this call's process tree.
                state.add_cancel_hook(lambda: _cancel_call(call))
                # C3: pings keep flowing while Claude thinks in silence.
                for delta in _iter_with_heartbeat(call):
                    if delta is _HEARTBEAT:
                        yield SSE_PING
                        continue
                    state.check()
                    if delta:
                        full.append(delta)
                        yield _sse_text(delta)
                state.check()
                # A stream that ends without producing any text is a failure,
                # not an empty success (audit P2-1): raising here — one place
                # covering all three modes — aborts before any save, and the
                # last-resort handler turns it into the terminal SSE error.
                # A client disconnect instead raises GeneratorExit at the yield
                # above, so it can never reach (or be misreported by) this line.
                if not full:
                    raise RuntimeError("Claude returned an empty answer")
            finally:
                # Publish whatever we accumulated even if the loop raised, so any
                # cleanup path sees a consistent value (the raise still aborts the
                # mode's save/done steps below).
                out[0] = "".join(full)

        out = [""]  # accumulator shared with the helper above

        # A6: the classifier call(s) this run started, so they can be
        # cancelled when the run no longer needs them (bounded join expired,
        # client disconnected, answer failed). Anything run_fn returns that
        # has a cancel() (claude_cli.ClaudeRun does) is cancellable; test
        # fakes without one are simply left to finish.
        cls_calls: list = []
        cls_calls_lock = threading.Lock()
        cls_cancelled = [False]

        def _cancel_call(call) -> None:
            cancel = getattr(call, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:  # noqa: BLE001 - cancelling is best-effort
                    app.logger.exception("could not cancel the classifier call")

        def _classifier_run_fn(prompt, **kwargs):
            call = run_fn(prompt, **kwargs)
            with cls_calls_lock:
                cls_calls.append(call)
                late = cls_cancelled[0]
            if late:  # the run was already over when this call started
                _cancel_call(call)
            return call

        def _cancel_classifier() -> None:
            with cls_calls_lock:
                cls_cancelled[0] = True
                calls = list(cls_calls)
            for call in calls:
                _cancel_call(call)

        def _learned_topics() -> list:
            """Topics already studied in THIS language (plus legacy,
            language-agnostic ones), capped to the most recent
            LEARNED_TOPICS_CAP so the prompt stays bounded (audit6 P2-12)."""
            try:
                return topic_index.known_topics(limit=LEARNED_TOPICS_CAP, language=language)
            except Exception:  # noqa: BLE001 - index is best-effort
                return []

        def _record_topics(cls) -> None:
            try:
                topic_index.record(cls.problem_type, cls.topics, language=language)
            except Exception:  # noqa: BLE001 - recording is best-effort
                app.logger.exception("could not record topics")

        def _save_with_fallback(save, fallback_body):
            """Run the mode's ``save()`` (returns the saved paths) and return
            ``(paths, warning)``. B25: if it raises ``OSError`` (a path past
            the OS limit, a locked or unwritable folder, a full disk), the
            fully streamed doc is written to ``output/_unsorted/<hash>.md``
            instead of being thrown away, and ``warning`` tells the user where
            it went. If even that fails the run ends in an error that says so."""
            try:
                return save(), None
            except OSError as exc:
                app.logger.exception("save failed (mode=%s); using the fallback", mode)
                try:
                    path = storage.save_unsorted(fallback_body)
                except OSError as exc2:
                    raise RuntimeError(
                        f"the answer could not be saved ({exc}); the fallback save "
                        f"failed too ({exc2}). Copy it from the page before leaving."
                    ) from exc2
                return [path], (
                    f"Could not save to the usual library folder ({exc}). "
                    f"Saved to {path} instead."
                )

        def event_stream():
            save_warning = None
            try:
                state.check()  # B14: cancelled before the stream even started
                state.add_cancel_hook(_cancel_classifier)
                # 1) classify on a background thread (audit6 P2-4). The short
                #    classification round-trip used to complete BEFORE the first
                #    answer delta streamed, delaying every run by a full Claude
                #    call; its result is only needed at save time, so it now
                #    runs concurrently with the answer stream (same injected
                #    run_fn -> tests still cover it) on the cheap classifier
                #    model. The pre-seeded fallback in ``cls_holder`` keeps the
                #    run alive even if the thread dies: classify never raises by
                #    contract, but a crash here must degrade to "uncategorized",
                #    never abort the run.
                cls_holder = [classifier.Classification(classifier.FALLBACK_TYPE, [])]

                def _classify_in_background():
                    try:
                        cls_holder[0] = classifier.classify(
                            problem,
                            run_fn=_classifier_run_fn,
                            model=config.classifier_model(),
                        )
                    except Exception:  # noqa: BLE001 - fallback already seeded above
                        app.logger.exception("background classification failed")

                cls_thread = threading.Thread(
                    target=_classify_in_background,
                    name="leetcoach-classify",
                    daemon=True,
                )
                cls_thread.start()

                def _classification():
                    """Join the classifier thread and return its result.

                    A6: the join is bounded. The answer is complete and
                    streamed by now; a classifier that is still not back after
                    CLASSIFIER_JOIN_TIMEOUT (a wedged CLI, or a fake that never
                    returns) must not hold the save hostage for the full run
                    watchdog. Its call is cancelled and the run saves under
                    the fallback type."""
                    deadline = time.monotonic() + CLASSIFIER_JOIN_TIMEOUT
                    while cls_thread.is_alive():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        cls_thread.join(min(SSE_PING_INTERVAL, remaining))
                        if cls_thread.is_alive() and time.monotonic() < deadline:
                            yield SSE_PING  # C3: keep the stream alive meanwhile
                    if cls_thread.is_alive():
                        app.logger.warning(
                            "classifier still running after %ss; saving as %s",
                            CLASSIFIER_JOIN_TIMEOUT,
                            classifier.FALLBACK_TYPE,
                        )
                        _cancel_classifier()
                        return classifier.Classification(classifier.FALLBACK_TYPE, [])
                    return cls_holder[0]

                # 2) build the mode-specific prompt; 3) stream + accumulate;
                #    4) save with the mode's own storage call. Only these two
                #    bits differ between modes — the stream/accumulate/done
                #    plumbing is shared. ``verification`` (Answer/Guided) is the
                #    sandbox verdict reported in the stream, saved .md and done
                #    payload; it stays None when a mode doesn't verify.
                verification = None
                if mode == "answer":
                    prompt = prompts.build_answer(problem, tier=tier, language=language)
                    yield from _stream_and_accumulate(prompt)
                    body = out[0]
                    code = parsing.extract_code(body, language)

                    # SP5: best-effort sample-I/O verification. Stream a short
                    # verdict line; the saved reasoning .md gets the verdict
                    # PLUS the per-sample failure detail (audit6 P2-9).
                    result, verdict = yield from _call_with_heartbeat(
                        lambda: _verify_code(code, problem, language)
                    )
                    verification = verdict
                    yield _sse_text("\n\n" + verdict + "\n")
                    reasoning = (
                        body + "\n\n---\n\n**Verification:** " + verdict + "\n"
                        + _verification_detail(result)
                    )

                    cls = yield from _classification()  # join before the save
                    state.check()  # B14: a cancelled run saves nothing

                    def _save_answer():
                        code_path, reasoning_path = storage.save_answer(
                            problem,
                            cls.problem_type,
                            tier=tier,
                            language=language,
                            code=code,
                            reasoning=reasoning,
                        )
                        # B24: code_path is None when no code block was
                        # extracted (an empty code file would otherwise land
                        # in the library).
                        return [p for p in (code_path, reasoning_path) if p]

                    # The reasoning .md already carries the code block, so it
                    # alone is the fallback copy.
                    paths, save_warning = _save_with_fallback(_save_answer, reasoning)
                elif mode == "learning":
                    # SP5: feed already-learned topics so Claude skips/cross-links
                    # covered tech, then record this run's topics afterward.
                    # Capped to the most recent LEARNED_TOPICS_CAP so the
                    # prompt stays bounded as the index grows (audit6 P2-12).
                    learned = _learned_topics()
                    prompt = prompts.build_learning(
                        problem,
                        language=language,
                        already_learned_topics=learned or None,
                    )
                    yield from _stream_and_accumulate(prompt)
                    cls = yield from _classification()  # join before the save
                    state.check()  # B14: a cancelled run saves nothing
                    paths, save_warning = _save_with_fallback(
                        lambda: [storage.save_learning(problem, cls.problem_type, out[0])],
                        out[0],
                    )
                    _record_topics(cls)
                else:  # mode == "guided" (validation guarantees a valid tier)
                    # B22: Guided teaches the stack too - skip what is known,
                    # and remember what this run covered.
                    learned = _learned_topics()
                    prompt = prompts.build_guided(
                        problem,
                        tier=tier,
                        language=language,
                        already_learned_topics=learned or None,
                    )
                    yield from _stream_and_accumulate(prompt)
                    body = out[0]

                    # SP5: verify Guided's answer step the same way as Answer —
                    # extract the code from the full piped doc exactly once
                    # (P2-13), and save verdict + failure detail (P2-9).
                    code = parsing.extract_code(body, language)
                    result, verdict = yield from _call_with_heartbeat(
                        lambda: _verify_code(code, problem, language)
                    )
                    verification = verdict
                    yield _sse_text("\n\n" + verdict + "\n")
                    saved = (
                        body + "\n\n---\n\n**Verification:** " + verdict + "\n"
                        + _verification_detail(result)
                    )
                    cls = yield from _classification()  # join before the save
                    state.check()  # B14: a cancelled run saves nothing
                    paths, save_warning = _save_with_fallback(
                        lambda: [storage.save_guided(problem, cls.problem_type, saved)],
                        saved,
                    )
                    _record_topics(cls)

                # A new artifact just landed under output/ — drop the library
                # cache so the next /library (the frontend refreshes right after
                # a run) reflects it even for a nested save the root mtime misses.
                _invalidate_library_cache()

                # 5) terminal success event
                done_payload = {
                    "mode": mode,
                    "problem_type": cls.problem_type,
                    "topics": cls.topics,
                    "paths": paths,
                }
                if verification is not None:
                    done_payload["verification"] = verification
                if save_warning:
                    done_payload["save_warning"] = save_warning
                yield _sse_event("done", done_payload)
            except Exception as exc:  # noqa: BLE001 - last-resort: always close cleanly
                if state.cancelled:
                    # B14: Stop -> POST /run/cancel. Whatever the killed call
                    # raised (ClaudeCancelledError, _RunCancelled), say so.
                    yield _sse_event("error", "Run cancelled.")
                else:
                    # Keep the full traceback in the server log (audit P2-8);
                    # the client still gets only the short message below.
                    app.logger.exception("run failed (mode=%s)", mode)
                    yield _sse_event("error", f"Run failed: {exc}")
            finally:
                # A6: a classifier call still running is no longer needed -
                # the client left (GeneratorExit), the answer failed, or the
                # save is done - so stop its `claude` process. A no-op for a
                # call that already finished.
                _cancel_classifier()
                # Release the in-flight key no matter how the stream ends —
                # normal completion, error, or a client disconnect (which raises
                # GeneratorExit here, bypassing the except above). Frees an
                # identical run to start again.
                _release_run(run_id, state)

        return Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable proxy buffering if present
                "X-Run-Id": run_id,
            },
        )

    @app.post("/run/cancel")
    def cancel_run():
        # B14: Stop in the UI. Kills the named run's Claude call(s) and frees
        # its in-flight slot at once, so re-running the same settings is not
        # a 409 until the server happens to notice the dropped connection.
        data, err = _json_object()
        if err:
            return err
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            return jsonify({"error": "A valid run_id is required."}), 400
        with _inflight_lock:
            state = _runs.get(run_id)
        if state is None:
            return jsonify({"cancelled": False}), 404
        state.cancel()
        _release_run(run_id, state)
        return jsonify({"cancelled": True})

    @app.post("/ask")
    def ask():
        # Quick Ask (SP2): a small syntax/stdlib question answered by the cheap
        # quick-ask model via the SAME injected run_fn as /run. The answer is
        # ephemeral — plain JSON, no SSE, nothing saved to the library.
        data, err = _json_object()
        if err:
            return err

        # Type-check before any .strip()/.lower() OR the problem-context slice
        # below (which sits outside the try/except): a non-string field must be
        # a clean 400, not a 500 (3.12), mirroring /run.
        type_err = _non_string_field_error(
            data,
            (("question", "Question"), ("language", "Language"),
             ("problem", "Problem")),
        )
        if type_err:
            return jsonify({"error": type_err}), 400

        question = (data.get("question") or "").strip()
        language = (data.get("language") or "").strip().lower() or "python"
        problem = data.get("problem") or ""

        # --- validation (reject before any Claude call, mirroring /run)
        if not question:
            return jsonify({"error": "A question is required."}), 400
        if len(question) > QUICK_ASK_MAX_QUESTION:
            return jsonify(
                {"error": f"Question too long (max {QUICK_ASK_MAX_QUESTION} chars)."}
            ), 400
        if language not in LANGUAGES:
            return jsonify({"error": f"Unknown language {language!r}."}), 400

        prompt = prompts.build_quick_ask(
            question,
            language=language,
            problem=problem[:QUICK_ASK_PROBLEM_CONTEXT_CAP],
        )
        try:
            answer = "".join(
                run_fn(
                    prompt,
                    model=config.quick_ask_model(),
                    system_prompt=prompts.QUICK_ASK_SYSTEM_PROMPT,
                    persist_session=False,  # A7: utility call, never resumed
                )
            ).strip()
        except Exception as exc:  # noqa: BLE001 - surface as a clean 502, log the rest
            app.logger.exception("quick ask failed")
            return jsonify({"error": f"Quick Ask failed: {exc}"}), 502
        if not answer:
            # Same stance as /run's empty-stream guard: no text is a failure,
            # not an empty success.
            return jsonify({"error": "Claude returned an empty answer."}), 502
        return jsonify({"answer": answer})

    return app


# Module-level app for `flask run` / WSGI servers (real claude runner).
app = create_app()


def _browser_url(host: str, port: int) -> str:
    """The URL a browser should open for a server bound to ``host:port``.
    A wildcard bind (0.0.0.0 / ::) is not browsable, so it maps to loopback;
    an IPv6 literal is bracketed."""
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/"


def _default_opener(url: str, timeout: float):
    # No proxies: a loopback probe must never be routed through HTTP_PROXY.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout)


def _existing_instance_url(host: str, port: int, *, timeout: float = 1.0, opener=None):
    """D16/B11: the URL of a LeetCoach already serving on ``host:port``, or
    ``None``. Asks ``/healthz`` and accepts only LeetCoach's own answer, so a
    different app on that port is never mistaken for it."""
    url = _browser_url(host, port)
    opener = opener or _default_opener
    try:
        with opener(url + "healthz", timeout=timeout) as resp:
            data = json.loads(resp.read(4096).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("app") == "leetcoach":
        return url
    return None


def _browser_enabled() -> bool:
    return os.environ.get("LEETCOACH_NO_BROWSER", "").lower() not in {"1", "true", "yes"}


def main(*, open_browser=webbrowser.open, serve=None) -> int:
    """Launch LeetCoach (``python app.py`` / the desktop launcher).

    D16/B11: if LeetCoach is already running on the preferred port, open the
    browser there and exit - a second double-click must not start a second
    server on the next free port sharing ``output/`` (the per-process locks
    don't coordinate across processes). Only when nothing (or something other
    than LeetCoach) holds the port does it fall back to a nearby free port.
    ``open_browser`` / ``serve`` are injectable for tests.
    """
    host = HOST
    existing = _existing_instance_url(host, PORT)
    if existing:
        print(f"LeetCoach is already running at  {existing}  - opening it.")
        if _browser_enabled():
            open_browser(existing)
        return 0
    # Fall back to a nearby free port instead of crashing when PORT is occupied
    # (another app on 5000) — a double-click launch must never die on "address
    # already in use".
    port = _choose_port(PORT, host)
    # Migrate any pre-rename answer files (simple/complex -> basic/optimal) so an
    # existing library keeps working after the "Code Quality" rename. Idempotent
    # and guarded: a failure here must never stop the app from launching.
    try:
        renamed = storage.migrate_tier_suffixes()
        if renamed:
            print(f"Migrated {len(renamed)} saved answer file(s) to the new tier names.")
    except Exception as exc:  # noqa: BLE001 - a migration hiccup must not block launch
        print(f"WARNING: could not migrate old tier filenames ({exc}).")
    # Clear sandbox temp dirs a crashed/killed run left behind (B6).
    swept = _sweep_sandbox_temp()
    if swept:
        print(f"Removed {swept} stale sandbox temp dir(s).")
    # Surface the CLI sign-in state so a terminal launch is guided too (the
    # launcher script handles the interactive `claude auth login`; here we only
    # tell the user what to do).
    try:
        # Also primes the B1 cache, so the first page load is instant.
        auth = claude_cli.cached_auth_status()
    except Exception:  # noqa: BLE001 - the probe must never stop the app launching
        auth = claude_cli.AuthStatus(installed=False, logged_in=False)
    if not auth.installed:
        print(
            "WARNING: the `claude` CLI was not found on PATH. The page will load "
            "but runs will fail until Claude Code is installed (or set "
            "LEETCOACH_CLAUDE_BIN)."
        )
    elif not auth.logged_in:
        print(
            "WARNING: you are signed out of the `claude` CLI. Runs will fail until "
            "you sign in — run `claude auth login` in a terminal, then reload."
        )
    url = _browser_url(host, port)
    print(f"LeetCoach running at  {url}  (Ctrl-C to stop)")
    # Auto-open the browser shortly after the server starts accepting connections
    # (the ~1s delay lets the server bind first). Suppressed for headless/dev use
    # via LEETCOACH_NO_BROWSER.
    if _browser_enabled():
        threading.Timer(1.0, lambda: open_browser(url)).start()
    if serve is None:
        app.run(host=host, port=port, debug=False, threaded=True)
    else:
        serve(port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
