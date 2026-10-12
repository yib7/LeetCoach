"""Starting LeetCoach: ``python app.py`` and the desktop launcher end up in
:func:`main`.

It reuses a LeetCoach already running in the port span instead of starting a
second server on the same ``output/`` (D16), otherwise picks a
free port near :data:`PORT`, does the startup housekeeping (old tier file
names, stale sandbox temp dirs), reports the ``claude`` sign-in state, opens the
browser and serves the Flask app from :mod:`leetcoach.app`.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
import urllib.request
import webbrowser

from leetcoach import app as webapp
from leetcoach import claude_cli, sandbox, storage

# Bind address for `python app.py` (single source of truth - the Host-header
# allowlist in request_guard keys off loopback hostnames, so no port is
# duplicated there).
HOST = "127.0.0.1"
PORT = 5000


def sweep_sandbox_temp() -> int:
    """Startup housekeeping (B6): remove ``leetcoach_run_*`` sandbox dirs
    older than a day that a crashed or killed run left in the temp dir.
    Returns how many were removed; never raises, so a hiccup can't block
    launch."""
    try:
        return sandbox.sweep_stale_run_dirs()
    except Exception:  # noqa: BLE001 - housekeeping must never stop the app launching
        return 0


PORT_SPAN = 20  # how far past PORT the fallback (and the instance probe) looks


def port_is_free(host: str, port: int) -> bool:
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


def choose_port(preferred: int, host: str, *, span: int = PORT_SPAN) -> int:
    """Pick a bindable TCP port on ``host``, preferring ``preferred``.

    Probe-bind a fresh ``SOCK_STREAM`` socket (address family derived from ``host``,
    so an IPv6 ``HOST`` such as ``::1`` works as well as IPv4) with NO
    ``SO_REUSEADDR`` (so probing an in-use port fails); if ``preferred``
    binds, return it. Otherwise scan ``preferred+1 .. preferred+span`` (clamped to the
    valid ``<= 65535`` range) and return the first port that binds. If the whole span
    is busy, bind to port ``0`` and return the OS-assigned ephemeral port. Every probe
    socket is closed before returning. A tiny TOCTOU window between the probe and
    ``app.run`` is acceptable for a single-user localhost tool: this only turns a
    hard crash on an occupied port into a graceful fallback."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET

    # Clamp the scan so a candidate never exceeds the valid port range (a high
    # PORT would otherwise raise OverflowError, not OSError, past 65535).
    for candidate in range(preferred, min(preferred + span, 65535) + 1):
        if port_is_free(host, candidate):
            return candidate
    # Whole span occupied (or preferred out of range): take an OS-assigned ephemeral port.
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def browser_url(host: str, port: int) -> str:
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


def existing_instance_url(host: str, port: int, *, timeout: float = 1.0, opener=None):
    """D16: the URL of a LeetCoach already serving on ``host:port``, or
    ``None``. Asks ``/healthz`` and accepts only LeetCoach's own answer, so a
    different app on that port is never mistaken for it."""
    url = browser_url(host, port)
    opener = opener or _default_opener
    try:
        with opener(url + "healthz", timeout=timeout) as resp:
            data = json.loads(resp.read(4096).decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        # A non-HTTP listener (say Redis) in the probe span makes
        # urllib raise BadStatusLine, an HTTPException rather than OSError.
        return None
    if isinstance(data, dict) and data.get("app") == "leetcoach":
        return url
    return None


def find_existing_instance(
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
    """The URL of a LeetCoach already serving anywhere in the
    port span :func:`choose_port` would fall back across, or ``None``.

    An earlier instance that found ``preferred`` taken by another app is on a
    fallback port, so probing only ``preferred`` would start a second server.
    A port that is free (nothing listening) is skipped without a request -
    on Windows a connect to a closed loopback port can take a second or two
    to be refused. Each probe has a short timeout (``timeout`` for the
    preferred port, ``fallback_timeout`` for the rest) and the whole scan an
    overall ``budget``, so a port held by something that never answers can't
    hang the launch."""
    is_free = is_free or port_is_free
    deadline = clock() + budget
    for port in range(preferred, min(preferred + span, 65535) + 1):
        if is_free(host, port):
            continue
        remaining = deadline - clock()
        if remaining <= 0:
            break
        per_probe = timeout if port == preferred else fallback_timeout
        url = existing_instance_url(
            host, port, timeout=min(per_probe, remaining), opener=opener
        )
        if url:
            return url
    return None


def browser_enabled() -> bool:
    return os.environ.get("LEETCOACH_NO_BROWSER", "").lower() not in {"1", "true", "yes"}


def main(*, open_browser=webbrowser.open, serve=None) -> int:
    """Launch LeetCoach (``python app.py`` / the desktop launcher).

    D16: if LeetCoach is already running on the preferred port, or on a
    fallback port in the span, open the browser there and exit. A second
    double-click must not start a second server on the next free port sharing
    ``output/`` (the per-process locks don't coordinate across processes).
    Only when no LeetCoach answers does it fall back to a nearby free port.
    ``open_browser`` / ``serve`` are injectable for tests.
    """
    host = HOST
    existing = find_existing_instance(host, PORT)
    if existing:
        print(f"LeetCoach is already running at  {existing}  - opening it.")
        if browser_enabled():
            open_browser(existing)
        return 0
    # Fall back to a nearby free port instead of crashing when PORT is occupied
    # (another app on 5000). A double-click launch must never die on "address
    # already in use".
    port = choose_port(PORT, host)
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
    swept = sweep_sandbox_temp()
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
            "you sign in: run `claude auth login` in a terminal, then reload."
        )
    url = browser_url(host, port)
    print(f"LeetCoach running at  {url}  (Ctrl-C to stop)")
    # Auto-open the browser shortly after the server starts accepting connections
    # (the ~1s delay lets the server bind first). Suppressed for headless/dev use
    # via LEETCOACH_NO_BROWSER.
    if browser_enabled():
        threading.Timer(1.0, lambda: open_browser(url)).start()
    if serve is None:
        # load_dotenv=False: Flask would re-read .env from the cwd as
        # strict UTF-8, crashing on a UTF-16 one that app._maybe_load_dotenv
        # handles, and ignoring LEETCOACH_NO_DOTENV.
        webapp.app.run(host=host, port=port, debug=False, threaded=True, load_dotenv=False)
    else:
        serve(port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
