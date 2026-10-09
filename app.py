"""Flask web layer for LeetCoach: a single page, the SSE `/run` and `/followup`
endpoints, the library browser (`/library`, `/library/file` GET + DELETE), the
problem store and review queue (`/problems`, `/review`), the re-attempt sandbox
(`/attempt/test`), flashcards, `/stats`, and the plain-JSON Quick Ask `/ask`.

Design mirrors the Xeno RAG pattern, in Flask flavour:

* ``create_app(*, run_fn=claude_cli.run)`` is a factory with an **injectable**
  Claude runner. The SAME ``run_fn`` is threaded into BOTH the classifier and
  the answer stream, so a single injected fake (tests) covers every Claude call
  while the real orchestration + save still runs end-to-end.
* ``/run`` returns ``Response(stream_with_context(event_stream()),
  mimetype="text/event-stream")``. The generator yields ``data:`` text events
  for each delta and a terminal ``event: done`` (or ``error`` / ``cancelled``) so the
  stream always closes cleanly — a last-resort ``except`` guarantees it.

All four modes (Answer / Learning / Guided / Code Review) are wired here. They share one
shape — classify -> build a mode-specific prompt -> stream + accumulate the
deltas -> save the result -> emit a terminal ``done`` — so the streaming and the
``done``/``error`` plumbing live in one place (``event_stream`` +
``_stream_and_accumulate``); only the per-mode prompt-builder and save call
differ.

SSE event protocol (shared by every mode):
    data: "<text delta>"\n\n                 # incremental answer text (json string)
    event: phase\ndata: {json}\n\n            # progress (SP5 D11), any number:
        {"phase": "streaming"} | {"phase": "verifying"[, "i": int, "n": int]}
        | {"phase": "saving"}
    event: meta\ndata: {"model": str}\n\n    # the concrete model id from the
                                              # CLI's system/init (SP5), once
    event: done\ndata: {json}\n\n             # terminal success:
        { "problem_type": str, "topics": [str], "paths": [str], "mode": str,
          "verification": str (Answer/Guided only — the sandbox verdict line),
          "model": str (when the CLI reported one) }
    event: error\ndata: "<message>"\n\n        # terminal failure (json string)
    event: cancelled\ndata: "<message>"\n\n    # terminal: stopped via POST
                                              # /run/cancel (SP5 fix B2) -
                                              # not a failure, nothing saved
    : ping\n\n                                # heartbeat comment (C3)

Clients must ignore event names they do not know (phase/meta are additive).

``POST /followup`` (SP8 D6) uses the same framing: text deltas, ``meta``,
``phase`` ({"phase": "streaming", "source": "resume"|"fallback"[, "reason"]}
then {"phase": "saving"}), a terminal ``done`` {"path", "source", "resumed",
"heading"[, "reason", "model"]} / ``error`` / ``cancelled`` (via
``POST /followup/cancel``), and the ``: ping`` heartbeat.
"""
from __future__ import annotations

import contextlib
import http.client
import inspect
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
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context

import classifier
import claude_cli
import config
import flashcards
import parsing
import practice
import problem_store
import prompts
import sandbox
import stats
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
VERSION = "1.5.0"

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
# novel can't balloon the Quick Ask prompt (LEETCOACH_QUICK_ASK_MODEL, Haiku
# by default).
QUICK_ASK_MAX_QUESTION = 500
QUICK_ASK_PROBLEM_CONTEXT_CAP = 6000

# SP8 / D6: a follow-up question on a saved doc. Longer than a Quick Ask (it
# can quote a line of the doc or some code) but still a question, not a paste.
FOLLOWUP_MAX_QUESTION = 2000

# SP7 fix 6: how long a new "Test my code" waits for a CANCELLED test to
# release the one-at-a-time slot (the sandbox kills its child within a poll
# tick; this only bounds a pathological cleanup).
ATTEMPT_CANCEL_GRACE_S = 10.0

# GET /problems lists these fields of each record - everything but the
# (possibly long) statement and notes - plus ``run_count`` (saved runs: the
# run-log entries of the problem, merged aliases included) and ``file_count``
# (len(runs): the library files those runs produced; an Answer run adds two).
PROBLEM_SUMMARY_FIELDS = ("id", "number", "title", "difficulty", "pattern",
                          "created", "updated", "review", "runs", "aliases")

# Allowlists — never pass an arbitrary string downstream to prompts/storage.
MODES = ("answer", "learning", "guided", "review")
# Modes without a code-quality tier (Learning teaches; a Code Review critiques
# the learner's own code).
UNTIERED_MODES = ("learning", "review")
LANGUAGES = prompts.LANGUAGES          # ("python", "cpp", "java")
TIERS = prompts.TIERS                  # ("basic", "normal", "optimal")


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

    3A W2: ``silent=True`` only swallows ``ValueError``; a body of a few hundred
    thousand nested ``[`` makes the decoder raise ``RecursionError``, which
    would otherwise be an HTML 500 from every JSON route.
    """
    not_object = (jsonify({"error": "Request body must be a JSON object."}), 400)
    try:
        data = request.get_json(silent=True)
    except RecursionError:
        return {}, not_object
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, not_object
    return data, None


def _log_failure(logger, exc: BaseException, msg: str, *args) -> None:
    """Log a failed run / ask / follow-up. An expected CLI failure (not
    installed, signed out, offline: :class:`claude_cli.ClaudeUnavailableError`
    already says what is wrong, and the page shows it) is one warning line,
    not a traceback in the console window the launcher keeps open (3A G2);
    anything else keeps its full traceback (audit P2-8)."""
    if isinstance(exc, claude_cli.ClaudeUnavailableError):
        logger.warning(msg + ": %s", *args, exc)
    else:
        logger.exception(msg, *args)


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


def _attempt_summary(value) -> tuple[dict | None, str | None]:
    """SP7: the optional ``attempt`` summary a grade carries (what the last
    "Test my code" run showed) -> ``(clean dict or None, error or None)``.
    Only ``language`` (allowlisted) and small non-negative ``passed`` /
    ``total`` counts are kept - nothing free-form reaches the record."""
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, "Attempt must be an object."
    out: dict = {}
    language = value.get("language")
    if language is not None:
        if language not in LANGUAGES:
            return None, "Unknown attempt language."
        out["language"] = language
    for key in ("passed", "total"):
        n = value.get(key)
        if n is None:
            continue
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 1000:
            return None, f"Attempt {key} must be a small whole number."
        out[key] = n
    if "passed" in out and "total" in out and out["passed"] > out["total"]:
        return None, "Attempt passed cannot exceed total."
    return (out or None), None


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

    Unsafe methods (every state-changing route: /run, /followup, /ask,
    /attempt/test, the /problems grade + notes routes, /config/model,
    DELETE /library/file and the cancel routes) must come from this page: a browser
    sends ``Origin`` on them, and it must be this exact origin;
    ``Sec-Fetch-Site: cross-site`` is refused outright. A request with neither
    header (curl, scripts, the test client) is allowed - the threat is a
    hostile web page, which cannot strip them.

    ``GET /`` runs the CLI sign-in probe, so a cross-site page must not be able
    to trigger it with an ``<img>``/``<iframe>``/``fetch``; only a top-level
    navigation (the user following a link) is allowed cross-site. SP4 review
    M11: ``same-site`` is treated the same way - another app on a different
    localhost port is "same-site" (the port is not part of a site), so only
    ``same-origin`` and ``none`` (typed URL, bookmark) load it freely.
    """
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if method in _UNSAFE_METHODS:
        if site == "cross-site":
            return "Cross-site request refused."
        origin = headers.get("Origin")
        if origin is not None and not _same_origin(origin, host_header, scheme):
            return "Cross-origin request refused."
        return None
    if path == "/" and site in ("cross-site", "same-site"):
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
                    with contextlib.suppress(Exception):  # best-effort teardown
                        close()
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
                with contextlib.suppress(Exception):  # best-effort teardown
                    cancel()


def _call_with_heartbeat(fn, *, on_abandon=None, updates=None):
    """Run the blocking ``fn()`` on a helper thread, yielding ``SSE_PING``
    every ``SSE_PING_INTERVAL`` while it works; ``return`` its result (use
    with ``yield from`` inside an SSE generator) or re-raise its exception.

    If the consumer stops early (the client went away: ``GeneratorExit`` at a
    ping), ``on_abandon()`` is called so the work can be stopped instead of
    running out its clock unobserved (SP4 review M2: the sandbox).

    ``updates`` (SP5 D11): an optional ``queue.Queue`` of ready-made SSE
    frames the work posts while it runs (verification progress); they are
    yielded as they arrive, and each one also counts as keep-alive."""
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

    def drain():
        while updates is not None:
            try:
                yield updates.get_nowait()
            except queue.Empty:
                return

    try:
        if updates is None:
            while not done.wait(SSE_PING_INTERVAL):
                yield SSE_PING
        else:
            last = time.monotonic()
            while True:
                for frame in drain():
                    yield frame
                    last = time.monotonic()
                if done.is_set():
                    break
                wait = SSE_PING_INTERVAL - (time.monotonic() - last)
                if wait <= 0:
                    yield SSE_PING
                    last = time.monotonic()
                    continue
                done.wait(min(wait, 0.1))
            yield from drain()
    finally:
        if not done.is_set() and on_abandon is not None:
            with contextlib.suppress(Exception):  # best-effort teardown
                on_abandon()
    if "error" in box:
        raise box["error"]
    return box["value"]


class _RunCancelled(Exception):
    """Raised inside a /run stream once ``POST /run/cancel`` named it."""


class _RunState:
    """One in-flight /run (B14): its dedup key and the cancel hooks of every
    Claude call it started, so ``POST /run/cancel`` can kill them all.

    SP4 review M1: :meth:`commit` is the point of no return (the save). It and
    :meth:`cancel` decide under one lock, so exactly one of them wins: a run
    cancelled first saves nothing, and a cancel that arrives after the commit
    is refused (the run finishes and reports ``done``)."""

    def __init__(self, key) -> None:
        self.key = key
        self._lock = threading.Lock()
        self._cancelled = False
        self._committed = False
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

    def cancel(self) -> bool:
        """Cancel the run; ``False`` (and nothing fired) if it already
        committed to saving."""
        with self._lock:
            if self._committed:
                return False
            self._cancelled = True
            hooks = list(self._hooks)
        for hook in hooks:
            hook()
        return True

    def check(self) -> None:
        if self._cancelled:
            raise _RunCancelled()

    def commit(self) -> None:
        """Final cancel check before the save; after it, cancel() refuses."""
        with self._lock:
            if self._cancelled:
                raise _RunCancelled()
            self._committed = True


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


PORT_SPAN = 20  # how far past PORT the fallback (and the instance probe) looks


def _port_is_free(host: str, port: int) -> bool:
    """Whether a fresh ``SOCK_STREAM`` socket (no ``SO_REUSEADDR``) can bind
    ``host:port`` right now. The socket is always closed again."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _choose_port(preferred: int, host: str, *, span: int = PORT_SPAN) -> int:
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

    # Clamp the scan so a candidate never exceeds the valid port range (a high
    # PORT would otherwise raise OverflowError, not OSError, past 65535).
    for candidate in range(preferred, min(preferred + span, 65535) + 1):
        if _port_is_free(host, candidate):
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


def _accepts_kwarg(fn, name: str) -> bool:
    """True if ``fn`` can be called with keyword ``name`` (or **kwargs)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _verify_code(code: str, problem: str, language: str, *, cancel=None, progress=None):
    """Best-effort sandbox verification of pre-extracted ``code`` (the caller
    extracts exactly once — audit6 P2-13). Returns ``(result, verdict_line)``;
    never raises (a verifier hiccup must not break a run). ``result`` may be
    ``None`` if verification couldn't even start. ``cancel`` (a
    ``threading.Event``) stops the sandbox early (SP4 review M2); ``progress``
    (SP5 D11) is passed on when the verifier accepts it."""
    try:
        extra = {}
        if progress is not None and _accepts_kwarg(sandbox.verify_answer, "progress"):
            extra["progress"] = progress
        result = sandbox.verify_answer(code, problem, language, cancel=cancel, **extra)
        return result, _verification_line(result)
    except Exception as exc:  # noqa: BLE001 - verification is strictly best-effort
        return None, f"⚠ not auto-verified (verifier error: {exc})"


def _topic_index_resolved() -> Path | None:
    try:
        return config.topic_index_path().resolve()
    except OSError:
        return None


def _is_hidden(root: Path, path: Path, topic_index_file: Path | None = None) -> bool:
    """B19: True for app metadata that is never part of the library - any
    path with a dot-prefixed segment (``.leetcoach/``, temp files), the topic
    index (``topic_index.json`` or wherever ``LEETCOACH_TOPIC_INDEX`` points
    inside the output dir). ``path`` is inside ``root``; pass the resolved
    topic-index path when checking many files (else it is looked up)."""
    rel = path.relative_to(root)
    if any(part.startswith(".") for part in rel.parts):
        return True
    if rel.as_posix() == "topic_index.json":
        return True
    if topic_index_file is None:
        topic_index_file = _topic_index_resolved()
    return path == topic_index_file


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
    out = tuple(sorted(sig, key=lambda item: item[0]))
    # SP6 fix M4: the hidden metadata store (run log + problem records) feeds
    # each file's verdict / difficulty, so an append made outside the app
    # (another instance, a hand edit) must invalidate the listing too.
    try:
        meta = problem_store.store_signature(root=root)
    except Exception:  # noqa: BLE001 - a signature must never fail the listing
        meta = None
    return out + (("<meta>", meta),)


# SP5 B18: the sandbox verdict a saved doc recorded in its trailing
# "**Verification:** <line>" (written by /run for Answer and Guided). Keyed by
# path + (mtime_ns, size) so an unchanged file is read once per process.
# SP5 fix R2: only the block /run itself appends ("---", blank line,
# "**Verification:** <line>") counts - a "**Verification:**" line the MODEL
# wrote inside a doc (e.g. a Learning doc quoting the format) is not a verdict.
_VERIFICATION_LINE_RE = re.compile(
    r"(?:^|\n)---\n\n\*\*Verification:\*\*[ \t]*([^\n]+?)[ \t]*(?=\n|$)"
)
_SAMPLE_VERDICT_RE = re.compile(r"Sample tests (PASS|FAIL|ERROR)\b")
_FOLLOW_UP_RE = re.compile(r"^##[ \t]+Follow-up\b", re.MULTILINE | re.IGNORECASE)
_VERDICT_READ_CAP = 1024 * 1024
_verdict_cache: dict = {}
_verdict_lock = threading.Lock()


def verdict_from_text(text: str) -> str | None:
    """``pass`` / ``fail`` / ``error`` / ``not_verified`` from the app-written
    ``---`` / ``**Verification:**`` block in ``text``, or ``None`` when it has
    none (Learning docs, legacy files). Mirrors :func:`_verification_line`.

    SP5 fix R2: anything under a ``## Follow-up`` heading that comes AFTER the
    first verification block is a later addition to the saved run and never
    changes its verdict; the last block before that point wins."""
    text = (text or "").replace("\r\n", "\n")
    first = _VERIFICATION_LINE_RE.search(text)
    if first is None:
        return None
    follow_up = _FOLLOW_UP_RE.search(text, first.end())
    if follow_up is not None:
        text = text[: follow_up.start()]
    matches = _VERIFICATION_LINE_RE.findall(text)
    m = _SAMPLE_VERDICT_RE.search(matches[-1])
    if m:
        return m.group(1).lower()
    return "not_verified"


def verdict_from_line(line: str | None) -> str | None:
    """``pass`` / ``fail`` / ``error`` / ``not_verified`` for a
    :func:`_verification_line`, ``None`` when the run was not verified."""
    if line is None:
        return None
    m = _SAMPLE_VERDICT_RE.search(line)
    return m.group(1).lower() if m else "not_verified"


def _log_index(root: Path) -> dict:
    """SP6: path -> {verdict, problem_id, difficulty} from the run log;
    empty (doc fallback everywhere) if the log can't be read."""
    try:
        return problem_store.path_index(root=root)
    except Exception:  # noqa: BLE001 - the listing must never fail on metadata
        return {}


def _md_verdict(path: Path, stat) -> str | None:
    key = str(path)
    sig = (stat.st_mtime_ns, stat.st_size)
    with _verdict_lock:
        hit = _verdict_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_VERDICT_READ_CAP)
    except OSError:
        return None
    verdict = verdict_from_text(raw.decode("utf-8", errors="replace"))
    with _verdict_lock:
        _verdict_cache[key] = (sig, verdict)
    return verdict


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
    topic_index_file = _topic_index_resolved()
    log_index = _log_index(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in LIBRARY_EXTENSIONS:
            continue
        if _is_hidden(root, path, topic_index_file):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue  # vanished mid-walk — skip, never 500 a listing
        entry = {
            "path": path.relative_to(root).as_posix(),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }
        logged = log_index.get(entry["path"])
        if logged is not None:
            # SP6: the run log is the source of truth for a run it recorded.
            if logged.get("difficulty"):
                entry["difficulty"] = logged["difficulty"]
            if logged.get("problem_id"):
                entry["problem_id"] = logged["problem_id"]
            # SP6 fix O3: the record's own title / number for display.
            if logged.get("title"):
                entry["title"] = logged["title"]
            if isinstance(logged.get("number"), int):
                entry["number"] = logged["number"]
            if path.suffix.lower() == ".md" and logged.get("verdict"):
                entry["verdict"] = logged["verdict"]
        elif path.suffix.lower() == ".md":
            # SP5 B18 (legacy files the log never saw): parsed from the doc.
            verdict = _md_verdict(path, stat)
            if verdict:
                entry["verdict"] = verdict
        files.append(entry)
    _prune_verdict_cache(root, files)
    return files


def _prune_verdict_cache(root: Path, files: list[dict]) -> None:
    """SP5 fix R7: forget cached verdicts of docs under ``root`` that are no
    longer listed (deleted / renamed), so the cache cannot grow without bound
    as runs come and go. Entries for other roots are left alone."""
    listed = {str(root / f["path"]) for f in files}
    prefix = str(root)
    with _verdict_lock:
        stale = [k for k in _verdict_cache if k.startswith(prefix) and k not in listed]
        for key in stale:
            del _verdict_cache[key]


def _existing_runs(runs) -> list[str]:
    """SP8 fix M7: the paths of a problem record's ``runs`` that are still
    library files. The record on disk is never pruned (a delete in Explorer
    or a restored file is reflected either way); the response just stops
    pointing at docs that are gone, so counts and "show the solution" agree
    with the library."""
    if not isinstance(runs, list):
        return []
    return [p for p in runs if isinstance(p, str) and _resolve_library_file(p) is not None]


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
    _asks: dict = {}  # B20: ask_id -> [call or None, cancelled Event] for /ask/cancel
    _attempts: dict = {}  # SP7: test_id -> cancel Event for /attempt/cancel
    _attempt_slot = threading.Lock()  # SP7: one "Test my code" run at a time
    # SP7 fix 6: the running test's cancel Event. A test cancelled (e.g. the
    # learner opened another problem) still holds the slot while the sandbox
    # kills its child, so the next test waits briefly for it instead of 409.
    _attempt_running: dict = {"cancel": None}
    # SP8 / D6: follow-ups have their own slot - one at a time per app,
    # independent of /run (a follow-up may stream while a study run does).
    _followups: dict = {}  # followup_id -> _RunState, for /followup/cancel
    _inflight_lock = threading.Lock()

    def _cancel_ask_call(call) -> None:
        cancel = getattr(call, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # cancelling is best-effort
                app.logger.exception("could not cancel the quick ask call")

    def _release_run(run_id: str, state: _RunState) -> None:
        with _inflight_lock:
            if _inflight_runs.get(state.key) == run_id:
                del _inflight_runs[state.key]
            if _runs.get(run_id) is state:
                del _runs[run_id]

    # Library-listing cache (P2-6): the /library listing walk (rglob + a stat
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
        # C13: the Quick Ask tag names the model /ask really uses (escaped:
        # it comes from LEETCOACH_QUICK_ASK_MODEL), not a hardcoded "haiku".
        html = html.replace("__QUICK_ASK_MODEL__", html_escape(config.quick_ask_model()))
        html = html.replace("__FOLLOWUP_MAX_QUESTION__", str(FOLLOWUP_MAX_QUESTION))
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

    @app.get("/favicon.ico")
    def favicon():
        # The page links /static/favicon.svg, but a browser still asks for
        # /favicon.ico on pages without that <link> (a JSON route, a text/plain
        # library file), which used to 404 in the console. Same-origin, so the
        # CSP's img-src 'self' allows it.
        return app.send_static_file("favicon.svg")

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
        # Listing of the study library (SP10). Missing/empty output
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
        # SP8 fix M5: under the library write lock, so a delete never lands
        # between a follow-up append's read and its atomic write (which would
        # resurrect the doc) - the append then sees the doc gone and saves nothing.
        with storage.WRITE_LOCK:
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

    @app.get("/problems")
    def problems():
        # SP6 / D1: every problem record, without the (possibly long)
        # statement; GET /problems/<id> has the full record.
        # SP6 fix O1: ``runs`` (the record's library paths) is included;
        # ``run_count`` counts saved runs from the log, ``file_count`` files.
        # SP8 fix M7: ``runs`` / ``file_count`` list only files that still
        # exist; ``run_count`` keeps counting the append-only log (A8).
        counts: dict = {}
        for entry in problem_store.read_runs():
            pid = entry.get("problem_id")
            if isinstance(pid, str):
                counts[pid] = counts.get(pid, 0) + 1
        listing = []
        for rec in problem_store.list_problems():
            item = {k: rec.get(k) for k in PROBLEM_SUMMARY_FIELDS}
            runs = _existing_runs(rec.get("runs"))
            # 3A W3: a hand-edited record may hold non-string aliases; an
            # unhashable one would crash the set below, so keep strings only.
            raw_aliases = rec.get("aliases")
            aliases = ([a for a in raw_aliases if isinstance(a, str)]
                       if isinstance(raw_aliases, list) else [])
            item["runs"] = runs
            item["aliases"] = aliases
            item["run_count"] = sum(counts.get(i, 0) for i in {rec.get("id"), *aliases})
            item["file_count"] = len(runs)
            listing.append(item)
        return jsonify({"problems": listing})

    @app.get("/problems/<pid>")
    def problem_detail(pid):
        rec = problem_store.load_problem(pid)  # None for an invalid id too
        if rec is None:
            return jsonify({"error": "Not found."}), 404
        # An alias (an id merged into this record) resolves to the record.
        log = problem_store.runs_for(pid)
        # SP8 fix M7: a deleted doc drops out of ``runs`` (the log keeps it).
        return jsonify({**rec, "runs": _existing_runs(rec.get("runs")), "log": log})

    @app.get("/review")
    def review_queue():
        # SP7 / D4: the Console "Due today" panel and the Stats review counts.
        return jsonify(problem_store.review_summary())

    @app.post("/problems/<pid>/grade")
    def grade_problem(pid):
        # SP7 / D4: a self-grade after a re-attempt moves the problem's
        # Leitner box and due date (solo -> next box, hints -> same box,
        # peeked -> box 1).
        if not problem_store.valid_problem_id(pid):
            return jsonify({"error": "Not found."}), 404
        data, err = _json_object()
        if err:
            return err
        grade = data.get("grade")
        if grade not in problem_store.GRADES:
            return jsonify({"error": "Grade must be one of: "
                            + ", ".join(problem_store.GRADES) + "."}), 400
        attempt, attempt_err = _attempt_summary(data.get("attempt"))
        if attempt_err:
            return jsonify({"error": attempt_err}), 400
        result = problem_store.grade_problem(pid, grade, attempt=attempt)
        if result is None:
            return jsonify({"error": "Not found."}), 404
        _invalidate_library_cache()
        return jsonify(result)

    @app.put("/problems/<pid>/notes")
    def problem_notes(pid):
        # SP7 / D9: the notes editor in the library viewer (debounced saves).
        if not problem_store.valid_problem_id(pid):
            return jsonify({"error": "Not found."}), 404
        data, err = _json_object()
        if err:
            return err
        notes = data.get("notes")
        if not isinstance(notes, str):
            return jsonify({"error": "Notes must be text."}), 400
        notes = notes.replace("\r\n", "\n")
        if len(notes) > problem_store.NOTES_CAP:
            return jsonify({"error": f"Notes are too long (max {problem_store.NOTES_CAP} "
                            "characters)."}), 400
        rec = problem_store.set_notes(pid, notes)
        if rec is None:
            return jsonify({"error": "Not found."}), 404
        return jsonify({"ok": True, "id": rec["id"], "notes_updated": rec.get("notes_updated"),
                        "length": len(notes)})

    @app.post("/attempt/test")
    def attempt_test():
        # SP7 / D3: "Test my code" in the re-attempt view. Python only, run
        # through the hardened sandbox against the saved statement's samples
        # plus the learner's own cases. Plain JSON; one test run at a time
        # (the sandbox is CPU-heavy); POST /attempt/cancel {test_id} stops it.
        data, err = _json_object()
        if err:
            return err
        type_err = _non_string_field_error(
            data, (("problem_id", "Problem id"), ("code", "Code"),
                   ("language", "Language"), ("test_id", "Test id")))
        if type_err:
            return jsonify({"error": type_err}), 400
        pid = data.get("problem_id") or ""
        if not problem_store.valid_problem_id(pid):
            return jsonify({"error": "A valid problem_id is required."}), 400
        test_id = data.get("test_id")
        if test_id is not None and not _RUN_ID_RE.fullmatch(test_id):
            return jsonify({"error": "Invalid test id."}), 400
        language = (data.get("language") or "").strip().lower()
        if language not in LANGUAGES:
            return jsonify({"error": f"Unknown language {language!r}."}), 400
        code = (data.get("code") or "").replace("\r\n", "\n")
        if len(code) > practice.CODE_CAP:
            return jsonify({"error": f"Code is too long (max {practice.CODE_CAP} "
                            "characters)."}), 400
        include_samples = data.get("include_samples", True)
        if not isinstance(include_samples, bool):
            return jsonify({"error": "include_samples must be true or false."}), 400
        try:
            custom = practice.parse_custom_cases(data.get("cases"))
        except practice.CaseError as exc:
            return jsonify({"error": str(exc)}), 400
        rec = problem_store.load_problem(pid)
        if rec is None:
            return jsonify({"error": "Not found."}), 404
        if language != "python":
            return jsonify({"supported": False, "status": "not_supported",
                            "message": practice.unsupported_message(language)})
        if not code.strip():
            return jsonify({"error": "Write some code first."}), 400
        statement = rec.get("statement") or ""
        cases = (practice.sample_cases(statement) if include_samples else []) + custom
        if not cases:
            return jsonify({"error": "No test cases: the saved statement has no sample "
                            "Input/Output - add a case of your own."}), 400
        acquired = _attempt_slot.acquire(blocking=False)
        if not acquired:
            with _inflight_lock:
                running = _attempt_running["cancel"]
            if running is not None and running.is_set():
                acquired = _attempt_slot.acquire(timeout=ATTEMPT_CANCEL_GRACE_S)
        if not acquired:
            return jsonify({"error": "A test run is already in progress."}), 409
        cancel = threading.Event()
        try:
            with _inflight_lock:
                _attempt_running["cancel"] = cancel
                if test_id:
                    _attempts[test_id] = cancel
            result = practice.run_cases(code, cases, problem_text=statement, cancel=cancel)
        finally:
            with _inflight_lock:
                if test_id and _attempts.get(test_id) is cancel:
                    del _attempts[test_id]
                if _attempt_running["cancel"] is cancel:
                    _attempt_running["cancel"] = None
            _attempt_slot.release()
        if cancel.is_set():
            result["cancelled"] = True
        return jsonify({"supported": True, "problem_id": rec.get("id"), **result})

    @app.post("/attempt/cancel")
    def attempt_cancel():
        data, err = _json_object()
        if err:
            return err
        test_id = data.get("test_id")
        if not isinstance(test_id, str) or not _RUN_ID_RE.fullmatch(test_id):
            return jsonify({"error": "A valid test_id is required."}), 400
        with _inflight_lock:
            event = _attempts.get(test_id)
        if event is None:
            return jsonify({"cancelled": False}), 404
        event.set()  # the sandbox kills the running case within a poll tick
        return jsonify({"cancelled": True})

    def _flashcard_scope():
        """The ``problem_id`` / ``path`` filters of a flashcards request, or
        an error response."""
        pid = request.args.get("problem_id")
        if pid is not None and not problem_store.valid_problem_id(pid):
            return None, None, (jsonify({"error": "Invalid problem id."}), 400)
        path = request.args.get("path")
        if path is not None:
            resolved = _resolve_library_file(path)
            if resolved is None or resolved.suffix.lower() != ".md":
                return None, None, (jsonify({"error": "Not found."}), 404)
            path = resolved.relative_to(config.output_dir().resolve()).as_posix()
        if pid is not None:
            rec = problem_store.load_problem(pid)
            pid = rec.get("id") if rec else pid  # an alias names its record
        return pid, path, None

    @app.get("/flashcards")
    def flashcards_list():
        # SP7 / D10: the cards of every doc's ## Flashcards section (newest
        # doc first) for the in-app flip review; ?problem_id= / ?path= narrow.
        pid, path, err = _flashcard_scope()
        if err:
            return err
        root = config.output_dir().resolve()
        cards = flashcards.collect(root, _cached_library_files(), problem_id=pid, path=path)
        return jsonify({"cards": cards, "count": len(cards)})

    @app.get("/flashcards.tsv")
    def flashcards_tsv():
        # SP7 / D10: the same cards as an Anki-importable TSV download.
        pid, path, err = _flashcard_scope()
        if err:
            return err
        root = config.output_dir().resolve()
        cards = flashcards.collect(root, _cached_library_files(), problem_id=pid, path=path)
        return Response(
            flashcards.to_tsv(cards).encode("utf-8"),
            content_type="text/tab-separated-values; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="leetcoach-flashcards.tsv"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/stats")
    def stats_route():
        # SP6 / A8: computed from the run log, legacy files (no log entry)
        # counted per saved run by their own mtime.
        return jsonify(stats.compute_stats(
            problem_store.read_runs(), _cached_library_files()))

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
             ("run_id", "Run id"), ("code", "Code")),
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
        # SP4 review I1: the picker's highlighted button for a pinned id in
        # .env (claude-sonnet-4-5 -> "sonnet") runs the pinned id, not the
        # generic alias.
        model = config.resolve_run_model(model)
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
        # Learning / Code Review have no tier; Answer/Guided require a valid one.
        if mode not in UNTIERED_MODES and tier not in TIERS:
            return jsonify({"error": f"Unknown tier {tier!r}."}), 400
        if mode in UNTIERED_MODES:
            tier = ""
        # SP7 / D5: Code Review reviews the learner's own attempt ("code").
        attempt_code = ""
        if mode == "review":
            attempt_code = (data.get("code") or "").replace("\r\n", "\n")
            if not attempt_code.strip():
                return jsonify({"error": "Paste your code to review."}), 400
            if len(attempt_code) > practice.CODE_CAP:
                return jsonify({"error": f"Code is too long (max {practice.CODE_CAP} "
                                "characters)."}), 400

        # De-dup identical in-flight runs (P2-12). Register atomically after
        # validation; an exact duplicate that's still streaming gets a 409.
        run_key = (problem, mode, language, tier, model, attempt_code)
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
                yield _sse_event("phase", {"phase": "streaming"})
                call = run_fn(
                    prompt,
                    system_prompt=prompts.TUTOR_SYSTEM_PROMPT,
                    persist_session=True,
                    **study_kwargs,
                )
                # B14: POST /run/cancel kills this call's process tree.
                state.add_cancel_hook(lambda: _cancel_call(call, "study call"))
                # C3: pings keep flowing while Claude thinks in silence.
                for delta in _iter_with_heartbeat(call):
                    yield from _announce_model(call)
                    if delta is _HEARTBEAT:
                        yield SSE_PING
                        continue
                    state.check()
                    if delta:
                        full.append(delta)
                        yield _sse_text(delta)
                yield from _announce_model(call)
                # SP6: the session id (from system/init or result) goes into
                # the run log - D6's follow-up resumes it.
                session_id = getattr(call, "session_id", None)
                if isinstance(session_id, str) and session_id:
                    run_meta["session_id"] = session_id
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
        run_meta: dict = {}  # SP5: what the client is told about the run

        def _announce_model(call):
            """SP5 model chip: once the CLI has named the concrete model it
            runs (``ClaudeRun.model``, from system/init), send it to the
            client - the picker only knows the alias. Once per run."""
            model_id = getattr(call, "model", None)
            if isinstance(model_id, str) and model_id and "model" not in run_meta:
                run_meta["model"] = model_id
                yield _sse_event("meta", {"model": model_id})

        # A6: the classifier call(s) this run started, so they can be
        # cancelled when the run no longer needs them (bounded join expired,
        # client disconnected, answer failed). Anything run_fn returns that
        # has a cancel() (claude_cli.ClaudeRun does) is cancellable; test
        # fakes without one are simply left to finish.
        cls_calls: list = []
        cls_calls_lock = threading.Lock()
        cls_cancelled = [False]

        def _cancel_call(call, what: str = "classifier call") -> None:
            cancel = getattr(call, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:  # cancelling is best-effort
                    app.logger.exception("could not cancel the %s", what)

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
            except Exception:  # recording is best-effort
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

        def _verify(code):
            """Sandbox-verify ``code`` with heartbeats (C3). SP4 review M2: a
            Stop (POST /run/cancel) or a client disconnect sets ``stop``, and
            the sandbox kills the running solution at once instead of letting
            it run out its timeout unobserved."""
            stop = threading.Event()
            state.add_cancel_hook(stop.set)
            # SP5 D11: "verifying i/n" progress frames ride the heartbeat loop.
            updates: queue.Queue = queue.Queue()

            def progress(i, n):
                updates.put(_sse_event("phase", {"phase": "verifying", "i": i, "n": n}))

            yield _sse_event("phase", {"phase": "verifying"})
            verified = yield from _call_with_heartbeat(
                lambda: _verify_code(
                    code, problem, language, cancel=stop, progress=progress
                ),
                on_abandon=stop.set,
                updates=updates,
            )
            state.check()  # cancelled while verifying: report that, not a verdict
            return verified

        def _record_run(paths, verification, cls, doc):
            """SP6 / D1: upsert the problem record and append the run-log
            line. Best-effort: a metadata failure is logged, never fails a
            run whose files are already saved. Returns the problem id."""
            try:
                return problem_store.record_run(
                    problem,
                    mode=mode,
                    language=language,
                    tier=tier if mode == "answer" else None,  # O2: Answer only
                    model=run_meta.get("model") or model or config.model(),
                    verdict=verdict_from_line(verification),
                    paths=paths,
                    session_id=run_meta.get("session_id"),
                    duration_s=time.monotonic() - started,
                    pattern=cls.problem_type,
                    doc=doc,
                )
            except Exception:  # metadata is best-effort
                app.logger.exception("could not record the run (mode=%s)", mode)
                return None

        started = time.monotonic()

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
                    except Exception:  # fallback already seeded above
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
                # SP6 / D2: the number + difficulty parsed from the paste
                # (locally) feed the doc contract's header lines.
                paste_meta = problem_store.parse_problem(problem)
                if mode == "answer":
                    prompt = prompts.build_answer(
                        problem, tier=tier, language=language, meta=paste_meta
                    )
                    yield from _stream_and_accumulate(prompt)
                    body = out[0]
                    code = parsing.extract_code(body, language)

                    # SP5: best-effort sample-I/O verification. Stream a short
                    # verdict line; the saved reasoning .md gets the verdict
                    # PLUS the per-sample failure detail (audit6 P2-9).
                    result, verdict = yield from _verify(code)
                    verification = verdict
                    yield _sse_text("\n\n" + verdict + "\n")
                    reasoning = (
                        body + "\n\n---\n\n**Verification:** " + verdict + "\n"
                        + _verification_detail(result)
                    )

                    yield _sse_event("phase", {"phase": "saving"})  # SP5 D11
                    cls = yield from _classification()  # join before the save
                    state.commit()  # B14 / M1: cancel wins only before this point

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
                        meta=paste_meta,
                    )
                    yield from _stream_and_accumulate(prompt)
                    yield _sse_event("phase", {"phase": "saving"})  # SP5 D11
                    cls = yield from _classification()  # join before the save
                    state.commit()  # B14 / M1: cancel wins only before this point
                    paths, save_warning = _save_with_fallback(
                        lambda: [storage.save_learning(problem, cls.problem_type, out[0])],
                        out[0],
                    )
                    _record_topics(cls)
                elif mode == "guided":  # validation guarantees a valid tier
                    # B22: Guided teaches the stack too - skip what is known,
                    # and remember what this run covered.
                    learned = _learned_topics()
                    prompt = prompts.build_guided(
                        problem,
                        tier=tier,
                        language=language,
                        already_learned_topics=learned or None,
                        meta=paste_meta,
                    )
                    yield from _stream_and_accumulate(prompt)
                    body = out[0]

                    # SP5: verify Guided's answer step the same way as Answer —
                    # extract the code from the full piped doc exactly once
                    # (P2-13), and save verdict + failure detail (P2-9).
                    code = parsing.extract_code(body, language)
                    result, verdict = yield from _verify(code)
                    verification = verdict
                    yield _sse_text("\n\n" + verdict + "\n")
                    saved = (
                        body + "\n\n---\n\n**Verification:** " + verdict + "\n"
                        + _verification_detail(result)
                    )
                    yield _sse_event("phase", {"phase": "saving"})  # SP5 D11
                    cls = yield from _classification()  # join before the save
                    state.commit()  # B14 / M1: cancel wins only before this point
                    paths, save_warning = _save_with_fallback(
                        lambda: [storage.save_guided(problem, cls.problem_type, saved)],
                        saved,
                    )
                    _record_topics(cls)
                elif mode == "review":
                    # SP7 / D5: critique the learner's attempt (fenced as
                    # untrusted data like the problem); nothing is verified.
                    prompt = prompts.build_review(
                        problem, attempt_code, language=language, meta=paste_meta
                    )
                    yield from _stream_and_accumulate(prompt)
                    # The saved doc keeps the attempt it reviewed.
                    saved = (
                        out[0].rstrip("\n") + "\n\n---\n\n## Your attempt\n\n"
                        + storage.attempt_block(attempt_code, language)
                    )
                    yield _sse_event("phase", {"phase": "saving"})
                    cls = yield from _classification()  # join before the save
                    state.commit()  # B14 / M1: cancel wins only before this point
                    paths, save_warning = _save_with_fallback(
                        lambda: [storage.save_review(problem, cls.problem_type, saved)],
                        saved,
                    )

                problem_id = _record_run(paths, verification, cls, out[0])

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
                if run_meta.get("model"):
                    done_payload["model"] = run_meta["model"]
                if problem_id:
                    done_payload["problem_id"] = problem_id
                yield _sse_event("done", done_payload)
            except Exception as exc:  # noqa: BLE001 - last resort, logged by _log_failure
                if state.cancelled:
                    # B14: Stop -> POST /run/cancel. Whatever the killed call
                    # raised (ClaudeCancelledError, _RunCancelled), say so -
                    # as its own terminal `cancelled` event (SP5 fix B2): a
                    # user Stop is not a failure, and the page shows it as
                    # the neutral "Stopped" state, not "Run failed".
                    yield _sse_event("cancelled", "Run cancelled.")
                else:
                    # Keep the full traceback in the server log (audit P2-8);
                    # the client still gets only the short message below.
                    _log_failure(app.logger, exc, "run failed (mode=%s)", mode)
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

        resp = Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable proxy buffering if present
                "X-Run-Id": run_id,
            },
        )
        # A client that leaves before the generator ever starts never reaches
        # event_stream's finally; the response's close still frees the slot
        # (idempotent with that finally, and only for this run's own key).
        resp.call_on_close(lambda: _release_run(run_id, state))
        return resp

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
        if not state.cancel():
            # M1: too late - the run already committed to saving. It finishes
            # (and frees its own slot); the client keeps reading for `done`.
            return jsonify({"cancelled": False})
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
             ("problem", "Problem"), ("ask_id", "Ask id")),
        )
        if type_err:
            return jsonify({"error": type_err}), 400
        # B20: an optional client id so the page's Cancel (or its 60 s
        # timeout) can kill the call via POST /ask/cancel.
        ask_id = data.get("ask_id")
        if ask_id is not None and not _RUN_ID_RE.fullmatch(ask_id):
            return jsonify({"error": "Invalid ask id."}), 400

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
        cancelled = threading.Event()
        # SP5 fix R1: register the ask BEFORE run_fn, so a Cancel (or the
        # client timeout) that lands while the call is still starting finds
        # it and sets the flag instead of 404-ing; the call itself is filled
        # in (and cancelled, if the flag is already set) once run_fn returns.
        entry = [None, cancelled]
        if ask_id:
            with _inflight_lock:
                _asks[ask_id] = entry
        try:
            try:
                call = run_fn(
                    prompt,
                    model=config.quick_ask_model(),
                    system_prompt=prompts.QUICK_ASK_SYSTEM_PROMPT,
                    persist_session=False,  # A7: utility call, never resumed
                )
                with _inflight_lock:
                    entry[0] = call
                if cancelled.is_set():
                    _cancel_ask_call(call)
                    return jsonify({"error": "Quick Ask cancelled."}), 409
                answer = "".join(call).strip()
            finally:
                if ask_id:
                    with _inflight_lock:
                        if _asks.get(ask_id) is entry:
                            del _asks[ask_id]
        except Exception as exc:  # noqa: BLE001 - a clean 502, logged by _log_failure
            if cancelled.is_set():
                return jsonify({"error": "Quick Ask cancelled."}), 409
            _log_failure(app.logger, exc, "quick ask failed")
            return jsonify({"error": f"Quick Ask failed: {exc}"}), 502
        if cancelled.is_set():
            return jsonify({"error": "Quick Ask cancelled."}), 409
        if not answer:
            # Same stance as /run's empty-stream guard: no text is a failure,
            # not an empty success.
            return jsonify({"error": "Claude returned an empty answer."}), 502
        return jsonify({"answer": answer})

    @app.post("/ask/cancel")
    def cancel_ask():
        # B20: the Quick Ask Cancel button / 60 s client timeout. Kills the
        # named call's `claude` process so it doesn't run out its watchdog.
        data, err = _json_object()
        if err:
            return err
        ask_id = data.get("ask_id")
        if not isinstance(ask_id, str) or not _RUN_ID_RE.fullmatch(ask_id):
            return jsonify({"error": "A valid ask_id is required."}), 400
        with _inflight_lock:
            entry = _asks.get(ask_id)
            if entry is None:
                return jsonify({"cancelled": False}), 404
            entry[1].set()
            call = entry[0]  # None while run_fn is still starting (R1)
        if call is not None:
            _cancel_ask_call(call)
        return jsonify({"cancelled": True})

    def _release_followup(followup_id: str, state: _RunState) -> None:
        with _inflight_lock:
            if _followups.get(followup_id) is state:
                del _followups[followup_id]

    @app.post("/followup")
    def followup():
        # SP8 / D6: a follow-up question on a saved doc, streamed as SSE. The
        # study run's own session is resumed (`claude -p --resume <id>`, same
        # isolation + neutral cwd) so Claude still has the whole conversation;
        # without a usable session it falls back to a fresh isolated call with
        # the doc as context. The answer is appended to the doc under
        # "## Follow-up — <question>". Same event protocol as /run.
        data, err = _json_object()
        if err:
            return err
        type_err = _non_string_field_error(
            data,
            (("path", "Path"), ("question", "Question"), ("model", "Model"),
             ("followup_id", "Follow-up id")),
        )
        if type_err:
            return jsonify({"error": type_err}), 400
        question = (data.get("question") or "").replace("\r\n", "\n").strip()
        if not question:
            return jsonify({"error": "A question is required."}), 400
        if len(question) > FOLLOWUP_MAX_QUESTION:
            return jsonify(
                {"error": f"Question too long (max {FOLLOWUP_MAX_QUESTION} chars)."}
            ), 400
        model = (data.get("model") or "").strip().lower()
        if model and model not in config.ALLOWED_MODEL_ALIASES:
            return jsonify({"error": f"Unknown model {model!r}."}), 400
        model = config.resolve_run_model(model)
        followup_id = data.get("followup_id")
        if followup_id is None:
            followup_id = uuid.uuid4().hex
        elif not _RUN_ID_RE.fullmatch(followup_id):
            return jsonify({"error": "Invalid follow-up id."}), 400

        # The library's containment gate (traversal, absolute paths, hidden
        # .leetcoach/ metadata all 404), and only a Markdown doc takes one.
        resolved = _resolve_library_file(data.get("path") or "")
        if resolved is None or resolved.suffix.lower() != ".md":
            return jsonify({"error": "Not found."}), 404
        rel_path = resolved.relative_to(config.output_dir().resolve()).as_posix()
        try:
            raw_doc = resolved.read_bytes()
        except OSError:
            return jsonify({"error": "Not found."}), 404
        # SP8 fix M2: the append reads the doc strictly (re-encoding replaced
        # bytes would corrupt it), so a doc that is not UTF-8 is refused HERE,
        # before a Claude call is spent on an answer that could not be saved.
        try:
            doc_text = raw_doc.decode("utf-8")
        except UnicodeDecodeError:
            return jsonify({"error": "This doc is not valid UTF-8 text, so a follow-up "
                            "can't be added to it. Re-save it as UTF-8 and try again."}), 422
        doc_text = doc_text.replace("\r\n", "\n").replace("\r", "\n")
        try:
            logged = problem_store.session_for_doc(rel_path)
        except Exception:  # no log: a legacy doc, use the fallback
            app.logger.exception("could not read the run log for %s", rel_path)
            logged = None
        logged = logged or {}
        session_id = logged.get("session_id")
        if not (isinstance(session_id, str)
                and claude_cli.SESSION_ID_RE.fullmatch(session_id)):
            session_id = None

        state = _RunState(("followup", followup_id))
        with _inflight_lock:
            if _followups:
                return jsonify({"error": "A follow-up is already running."}), 409
            _followups[followup_id] = state

        run_meta: dict = {}
        started = time.monotonic()

        def _announce_model(call):
            model_id = getattr(call, "model", None)
            if isinstance(model_id, str) and model_id and "model" not in run_meta:
                run_meta["model"] = model_id
                yield _sse_event("meta", {"model": model_id})

        def _stream(prompt, acc, *, resume):
            """Stream one claude call's deltas (SSE text) into ``acc``."""
            kwargs = {
                "system_prompt": prompts.FOLLOWUP_SYSTEM_PROMPT,
                # The resumed study session keeps the follow-up turn (a later
                # follow-up sees it); the fresh fallback call is a utility call.
                "persist_session": resume is not None,
            }
            if resume is not None:
                kwargs["resume"] = resume
            if model:
                kwargs["model"] = model
            state.check()
            call = run_fn(prompt, **kwargs)
            state.add_cancel_hook(lambda: _cancel_ask_call(call))
            for delta in _iter_with_heartbeat(call):
                yield from _announce_model(call)
                if delta is _HEARTBEAT:
                    yield SSE_PING
                    continue
                state.check()
                if delta:
                    acc.append(delta)
                    yield _sse_text(delta)
            yield from _announce_model(call)
            state.check()

        def event_stream():
            try:
                state.check()
                acc: list = []
                source = None
                reason = None
                if session_id is None:
                    reason = "no_session"
                else:
                    yield _sse_event("phase", {"phase": "streaming", "source": "resume"})
                    try:
                        yield from _stream(prompts.build_followup(question), acc,
                                           resume=session_id)
                        source = "resume"
                    except claude_cli.ClaudeUnavailableError as exc:
                        # Fall back only when the resume failed BEFORE any text
                        # (session not found, a nonzero exit, a CLI without
                        # --resume). A sign-in / usage-limit failure would hit
                        # the fresh call too, so it is reported as is - and so
                        # is a watchdog timeout (SP8 fix M4): a second full call
                        # would double an already maximal wait.
                        if (acc or state.cancelled
                                or isinstance(exc, claude_cli.ClaudeTimeoutError)
                                or claude_cli.is_auth_or_limit_error(str(exc))):
                            raise
                        reason = ("unsupported"
                                  if isinstance(exc, claude_cli.ResumeUnsupportedError)
                                  else "resume_failed")
                        app.logger.info("follow-up resume failed (%s); falling back", exc)
                if source is None:
                    yield _sse_event(
                        "phase", {"phase": "streaming", "source": "fallback", "reason": reason}
                    )
                    acc = []
                    yield from _stream(prompts.build_followup(question, doc=doc_text), acc,
                                       resume=None)
                    source = "fallback"
                answer = "".join(acc).strip()
                if not answer:
                    raise RuntimeError("Claude returned an empty answer")
                yield _sse_event("phase", {"phase": "saving"})
                state.commit()  # cancel wins only before this point
                try:
                    heading = storage.append_followup(resolved, question, answer)
                except FileNotFoundError:
                    raise RuntimeError(
                        "the doc was moved or deleted while Claude answered; nothing was saved"
                    ) from None
                except UnicodeDecodeError:
                    raise RuntimeError(
                        "the doc was changed to non-UTF-8 text while Claude answered; "
                        "nothing was saved"
                    ) from None
                except OSError as exc:
                    raise RuntimeError(
                        f"could not add the answer to the doc ({exc.strerror or exc})"
                    ) from exc
                try:
                    problem_store.record_followup(
                        rel_path,
                        problem_id=logged.get("problem_id"),
                        resumed=source == "resume",
                        session_id=session_id if source == "resume" else None,
                        model=run_meta.get("model") or model or config.model(),
                        duration_s=time.monotonic() - started,
                    )
                except Exception:  # the doc already holds the answer
                    app.logger.exception("could not log the follow-up on %s", rel_path)
                _invalidate_library_cache()
                done = {"path": rel_path, "source": source, "resumed": source == "resume",
                        "heading": heading}
                if reason:
                    done["reason"] = reason
                if run_meta.get("model"):
                    done["model"] = run_meta["model"]
                yield _sse_event("done", done)
            except Exception as exc:  # noqa: BLE001 - last resort, logged by _log_failure
                if state.cancelled:
                    yield _sse_event("cancelled", "Follow-up cancelled.")
                else:
                    _log_failure(app.logger, exc, "follow-up failed on %s", rel_path)
                    yield _sse_event("error", f"Follow-up failed: {exc}")
            finally:
                _release_followup(followup_id, state)

        resp = Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Followup-Id": followup_id,
            },
        )
        resp.call_on_close(lambda: _release_followup(followup_id, state))
        return resp

    @app.post("/followup/cancel")
    def cancel_followup():
        # SP8 / D6: Stop / Esc on the follow-up box. Same contract as
        # /run/cancel: 200 {"cancelled": true}; 200 {"cancelled": false} once
        # the answer is being appended (too late); 404 unknown; 400 invalid.
        data, err = _json_object()
        if err:
            return err
        followup_id = data.get("followup_id")
        if not isinstance(followup_id, str) or not _RUN_ID_RE.fullmatch(followup_id):
            return jsonify({"error": "A valid followup_id is required."}), 400
        with _inflight_lock:
            state = _followups.get(followup_id)
        if state is None:
            return jsonify({"cancelled": False}), 404
        if not state.cancel():
            return jsonify({"cancelled": False})
        _release_followup(followup_id, state)
        return jsonify({"cancelled": True})

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
    except (OSError, ValueError, http.client.HTTPException):
        # 3A W1: a non-HTTP listener (say Redis) in the probe span makes
        # urllib raise BadStatusLine, an HTTPException rather than OSError.
        return None
    if isinstance(data, dict) and data.get("app") == "leetcoach":
        return url
    return None


def _find_existing_instance(
    host: str,
    preferred: int,
    *,
    span: int = PORT_SPAN,
    timeout: float = 1.0,
    fallback_timeout: float = 0.5,
    budget: float = 3.0,
    opener=None,
    is_free=None,
    clock=time.monotonic,
):
    """SP4 review M10: the URL of a LeetCoach already serving anywhere in the
    port span :func:`_choose_port` would fall back across, or ``None``.

    An earlier instance that found ``preferred`` taken by another app is on a
    fallback port, so probing only ``preferred`` would start a second server.
    A port that is free (nothing listening) is skipped without a request -
    on Windows a connect to a closed loopback port can take a second or two
    to be refused. Each probe has a short timeout (``timeout`` for the
    preferred port, ``fallback_timeout`` for the rest) and the whole scan an
    overall ``budget``, so a port held by something that never answers can't
    hang the launch."""
    is_free = is_free or _port_is_free
    deadline = clock() + budget
    for port in range(preferred, min(preferred + span, 65535) + 1):
        if is_free(host, port):
            continue
        remaining = deadline - clock()
        if remaining <= 0:
            break
        per_probe = timeout if port == preferred else fallback_timeout
        url = _existing_instance_url(
            host, port, timeout=min(per_probe, remaining), opener=opener
        )
        if url:
            return url
    return None


def _browser_enabled() -> bool:
    return os.environ.get("LEETCOACH_NO_BROWSER", "").lower() not in {"1", "true", "yes"}


def main(*, open_browser=webbrowser.open, serve=None) -> int:
    """Launch LeetCoach (``python app.py`` / the desktop launcher).

    D16/B11: if LeetCoach is already running on the preferred port - or on a
    fallback port in the span (M10) - open the browser there and exit - a
    second double-click must not start a second server on the next free port
    sharing ``output/`` (the per-process locks don't coordinate across
    processes). Only when no LeetCoach answers does it fall back to a nearby
    free port.
    ``open_browser`` / ``serve`` are injectable for tests.
    """
    host = HOST
    existing = _find_existing_instance(host, PORT)
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
        # load_dotenv=False (3A G1): Flask would re-read .env from the cwd as
        # strict UTF-8, crashing on a UTF-16 one that _maybe_load_dotenv
        # handles, and ignoring LEETCOACH_NO_DOTENV.
        app.run(host=host, port=port, debug=False, threaded=True, load_dotenv=False)
    else:
        serve(port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
