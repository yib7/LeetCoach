"""Server-sent-events plumbing shared by the streaming routes (``/run``,
``/followup``): the frame formatters, the heartbeat that keeps a quiet stream
alive and notices a client that went away, and the cancel/commit state of one
in-flight run. The event protocol itself is documented in :mod:`leetcoach.app`.
"""
from __future__ import annotations

import contextlib
import json
import queue
import threading
import time

# C3: an SSE comment frame sent while the stream is otherwise silent (Claude
# thinking, the sandbox verifying, the classifier join). It keeps proxies and
# the browser from timing the stream out, and (B14) a write to a client that
# has gone away fails, so the server notices the disconnect and
# frees the run instead of waiting for Claude to finish. Ignored by EventSource
# and by app.js's parser. The interval is read at call time (tests shorten it).
SSE_PING = ": ping\n\n"
SSE_PING_INTERVAL = 15.0

HEARTBEAT = object()
_ITEM, _DONE, _ERROR = "item", "done", "error"


def iter_with_heartbeat(iterable):
    """Yield the items of ``iterable`` - pulled on a helper thread - and
    ``HEARTBEAT`` whenever ``SSE_PING_INTERVAL`` passes with no new item.
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
                yield HEARTBEAT
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


def call_with_heartbeat(fn, *, on_abandon=None, updates=None):
    """Run the blocking ``fn()`` on a helper thread, yielding ``SSE_PING``
    every ``SSE_PING_INTERVAL`` while it works; ``return`` its result (use
    with ``yield from`` inside an SSE generator) or re-raise its exception.

    If the consumer stops early (the client went away: ``GeneratorExit`` at a
    ping), ``on_abandon()`` is called so the work can be stopped instead of
    running out its clock unobserved (the sandbox verifier uses this).

    ``updates`` (D11): an optional ``queue.Queue`` of ready-made SSE
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


class RunCancelled(Exception):
    """Raised inside a /run stream once ``POST /run/cancel`` named it."""


class RunState:
    """One in-flight /run (B14): its dedup key and the cancel hooks of every
    Claude call it started, so ``POST /run/cancel`` can kill them all.

    :meth:`commit` is the point of no return (the save). It and
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
            raise RunCancelled()

    def commit(self) -> None:
        """Final cancel check before the save; after it, cancel() refuses."""
        with self._lock:
            if self._cancelled:
                raise RunCancelled()
            self._committed = True



def sse_text(delta: str) -> str:
    """A streamed text delta. JSON-encoded so newlines/markdown survive transport
    (a raw newline is an SSE event boundary)."""
    return f"data: {json.dumps(delta)}\n\n"


def sse_event(name: str, payload) -> str:
    """A named terminal event carrying a JSON payload."""
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n"
