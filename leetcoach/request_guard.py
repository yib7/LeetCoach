"""Who may talk to LeetCoach: the checks every request passes before a route
runs (the ``before_request`` hook in :mod:`leetcoach.app` calls them).

* :func:`is_loopback_peer` - the TCP peer must be this machine (Phase 4), so a
  ``flask run --host 0.0.0.0`` or a LAN-bound WSGI server still serves no one
  else.
* :data:`ALLOWED_HOSTNAMES` / :func:`hostname` - the ``Host`` header must name
  loopback (the DNS-rebinding defense, audit P1-3).
* :func:`cross_site_rejection` - a state-changing request must come from this
  page's own origin, and a cross-site page cannot load ``/`` (C2, SP4 M11).
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

# Host-header allowlist (DNS-rebinding defense). Hostnames only, ANY port: a
# rebinding attacker controls what IP their hostname resolves to, never the
# hostname the victim's browser sends — so matching the hostname IS the whole
# defense, and pinning a port would only break `flask run` on a non-default
# port. Bracketed "[::1]" covers the IPv6 loopback literal.
ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})


def is_loopback_peer(addr: str | None) -> bool:
    """Phase 4: whether the TCP peer (``request.remote_addr``) is this machine.
    An IPv4-mapped IPv6 peer (``::ffff:127.0.0.1``) counts by its IPv4 form.
    A missing or unparseable address is not loopback (fail closed)."""
    try:
        ip = ipaddress.ip_address((addr or "").strip())
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


def hostname(host: str) -> str:
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
    host = hostname(hostport)
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


def cross_site_rejection(method: str, path: str, headers, host_header: str, scheme: str):
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
