"""``streaming.call_with_heartbeat``: a blocking call run off the request
thread forwards its progress frames, returns the call's value, and signals
``on_abandon`` only when the client walks away mid-call (SP4 fix M2)."""
from __future__ import annotations

import queue
import threading
import time

import pytest

from leetcoach import streaming


def test_call_with_heartbeat_yields_updates_then_returns():
    updates: queue.Queue = queue.Queue()

    def work():
        updates.put("frame-1")
        time.sleep(0.05)
        updates.put("frame-2")
        return 42

    gen = streaming.call_with_heartbeat(work, updates=updates)
    frames = []
    try:
        while True:
            frames.append(next(gen))
    except StopIteration as stop:
        assert stop.value == 42
    assert [f for f in frames if f.startswith("frame")] == ["frame-1", "frame-2"]


def test_abandoned_blocking_call_signals_its_cancel(monkeypatch):
    monkeypatch.setattr(streaming, "SSE_PING_INTERVAL", 0.01)
    release = threading.Event()
    abandoned = threading.Event()

    def work():
        release.wait(5)
        return 1

    gen = streaming.call_with_heartbeat(work, on_abandon=abandoned.set)
    assert next(gen) == streaming.SSE_PING
    gen.close()  # the client went away mid-verification
    assert abandoned.is_set()
    release.set()


def test_finished_blocking_call_does_not_signal_cancel(monkeypatch):
    abandoned = threading.Event()
    gen = streaming.call_with_heartbeat(lambda: 7, on_abandon=abandoned.set)
    with pytest.raises(StopIteration) as stop:
        while True:
            next(gen)
    assert stop.value.value == 7
    assert not abandoned.is_set()
