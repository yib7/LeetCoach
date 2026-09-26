"""Tests for the ``_choose_port`` launch helper (Cycle 10 SP-A).

``_choose_port(preferred, host, *, span=20)`` is a pure, module-level helper used
only by ``app.py``'s ``__main__`` block to pick a bindable port so a double-click
launch never crashes on an occupied port. These tests run offline with no browser
and no ``claude`` call — they only exercise real socket binds on loopback.
"""
from __future__ import annotations

import socket

from app import _choose_port

HOST = "127.0.0.1"


def _free_port() -> int:
    """Ask the OS for a free port, then release it. There is a tiny race between
    release and re-probe, but on an idle test host the port stays free."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def test_choose_port_returns_preferred_when_free():
    """A free preferred port is returned unchanged."""
    port = _free_port()
    assert _choose_port(port, HOST) == port


def test_choose_port_falls_back_when_busy():
    """When the preferred port is held, a *different* — and itself bindable —
    port is returned instead of crashing."""
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        held.bind((HOST, 0))
        busy_port = held.getsockname()[1]
        # The preferred port is occupied for the whole call, so it must fall back.
        chosen = _choose_port(busy_port, HOST)
        assert chosen != busy_port
        # The chosen port is genuinely bindable (no SO_REUSEADDR shortcut).
        confirm = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            confirm.bind((HOST, chosen))
        finally:
            confirm.close()
    finally:
        held.close()


def test_choose_port_high_preferred_stays_in_range():
    """A preferred port past the valid range must NOT raise OverflowError; the scan
    is clamped to <= 65535 and it falls back to an OS-assigned ephemeral port."""
    chosen = _choose_port(70000, HOST)
    assert isinstance(chosen, int)
    assert 1 <= chosen <= 65535


def test_choose_port_ipv6_host():
    """An IPv6 host (e.g. HOST='::1') is probed with an IPv6 socket rather than
    crashing on an AF_INET/AF_INET6 mismatch."""
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe.bind(("::1", 0))
        free6 = probe.getsockname()[1]
        probe.close()
    except OSError:
        import pytest

        pytest.skip("no IPv6 loopback on this host")
    chosen = _choose_port(free6, "::1")
    assert isinstance(chosen, int)
    assert 1 <= chosen <= 65535
