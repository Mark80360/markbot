"""SSRF (Server-Side Request Forgery) protection utilities.

Block lists are loaded from :class:`markbot.config.schema.SsrfConfig`
via :func:`init_from_config`. This module holds no hardcoded block
lists — all rules live in the Config schema so they can be overridden
per deployment.

Address normalisation
---------------------
``ipaddress`` compares addresses and networks *within a version*:
``IPv6Address == IPv4Address`` is always ``False``, and
``IPv6Address in IPv4Network`` silently returns ``False`` instead of
raising. A predicate built as "is this IP in the block list" therefore
waves through every IPv6 address that merely *encodes* a blocked IPv4
address — ``::ffff:169.254.169.254``, ``2002:a9fe:a9fe::`` (6to4),
``64:ff9b::a9fe:a9fe`` (NAT64). On a cloud host that is one prompt
injection away from the instance metadata service.

:func:`_embedded_ipv4` decodes those encodings so the *embedded* IPv4
address is what gets checked. Note that a blanket ``is_private``
fallback is **not** a substitute: CPython lists the whole
``::ffff:0:0/96`` block as private, so it would also block
``::ffff:8.8.8.8``, which is a legitimate public address. The fallback
in :func:`_v6_intrinsically_internal` is only consulted when the
address has no embedded IPv4.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from loguru import logger

from markbot.config.schema import Config

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset()
_ALWAYS_BLOCKED_IPS: tuple[ipaddress._BaseAddress, ...] = ()
_PRIVATE_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = ()
_INITIALIZED: bool = False

#: IPv4 addresses encoded into an IPv6 address by a transition mechanism.
#: ``::ffff:0:0/96`` is IPv4-mapped, ``::/96`` is the deprecated
#: IPv4-compatible form, ``2002::/16`` is 6to4 and ``64:ff9b::/96`` is
#: the NAT64 well-known prefix.
_IPV4_MAPPED = ipaddress.IPv6Network("::ffff:0:0/96")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_SIX_TO_FOUR = ipaddress.IPv6Network("2002::/16")
_NAT64_WKP = ipaddress.IPv6Network("64:ff9b::/96")


def _parse_ips(ip_strings: list[str]) -> tuple[ipaddress._BaseAddress, ...]:
    """Parse IP strings into ipaddress objects.

    Entries are stripped before parsing, because a single trailing space
    used to turn ``"169.254.169.254 "`` into an unparseable string that
    was dropped without a word — silently emptying the metadata block
    list. Anything still unparseable is reported at ERROR level: a block
    list that quietly shrank is a security downgrade, not a cosmetic
    config nit.
    """
    out: list[ipaddress._BaseAddress] = []
    for raw in ip_strings:
        s = (raw or "").strip()
        if not s:
            continue
        try:
            out.append(ipaddress.ip_address(s))
        except ValueError:
            logger.error(
                "SSRF: ignoring invalid always_blocked_ips entry {!r} — "
                "this address will NOT be blocked",
                raw,
            )
    if ip_strings and not out:
        logger.error(
            "SSRF: always_blocked_ips has {} entr(y/ies) but none parsed; "
            "cloud metadata addresses are NOT blocked",
            len(ip_strings),
        )
    return tuple(out)


def _parse_networks(net_strings: list[str]) -> tuple[ipaddress._BaseNetwork, ...]:
    """Parse CIDR strings into ip_network objects.

    Same strip-then-report contract as :func:`_parse_ips`.
    """
    out: list[ipaddress._BaseNetwork] = []
    for raw in net_strings:
        s = (raw or "").strip()
        if not s:
            continue
        try:
            out.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            logger.error(
                "SSRF: ignoring invalid blocked_networks entry {!r} — "
                "this network will NOT be blocked",
                raw,
            )
    if net_strings and not out:
        logger.error(
            "SSRF: blocked_networks has {} entr(y/ies) but none parsed; "
            "private networks are NOT blocked",
            len(net_strings),
        )
    return tuple(out)


def init_from_config(config: Config) -> None:
    """Populate SSRF block lists from Config.ssrf section."""
    global _BLOCKED_HOSTNAMES, _ALWAYS_BLOCKED_IPS, _PRIVATE_NETWORKS, _INITIALIZED
    ssrf_cfg = config.ssrf
    _BLOCKED_HOSTNAMES = frozenset(s.strip().lower() for s in ssrf_cfg.blocked_hostnames if s)
    _ALWAYS_BLOCKED_IPS = _parse_ips(ssrf_cfg.always_blocked_ips)
    _PRIVATE_NETWORKS = _parse_networks(ssrf_cfg.blocked_networks)
    _INITIALIZED = True


def _ensure_initialized() -> None:
    """Lazily apply default Config.ssrf if nobody called init_from_config yet.

    Production paths call :func:`init_from_config` from ``load_config``.
    This guard covers direct tool use / tests that never load config, so
    private-network and metadata blocks are never accidentally empty.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    init_from_config(Config())


def _resolve_hostname(hostname: str) -> list[ipaddress._BaseAddress]:
    """Resolve hostname to IPs. Returns empty list on failure."""
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        ips: set[ipaddress._BaseAddress] = set()
        for _family, _type, _proto, _canon, sockaddr in results:
            try:
                ips.add(ipaddress.ip_address(sockaddr[0]))
            except ValueError:
                continue
        return list(ips)
    except socket.gaierror:
        return []


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """Return every IPv4 address encoded inside *addr*.

    Covers IPv4-mapped (``::ffff:a.b.c.d``), the deprecated
    IPv4-compatible form (``::a.b.c.d``), 6to4 (``2002::/16``) and the
    NAT64 well-known prefix (``64:ff9b::/96``).

    Teredo (``2001::/32``) is deliberately **not** decoded: only the
    relay address is a plain IPv4, the client address is XOR-obfuscated,
    and the relay is a public server — blocking on it would produce
    false positives without closing a real path (Teredo needs a local
    tunnel interface to be reachable at all).
    """
    if not isinstance(addr, ipaddress.IPv6Address):
        return []

    found: list[ipaddress.IPv4Address] = []

    mapped = addr.ipv4_mapped
    if mapped is not None:
        found.append(mapped)
        return found

    packed = addr.packed
    if addr in _SIX_TO_FOUR:
        found.append(ipaddress.IPv4Address(packed[2:6]))
    if addr in _NAT64_WKP:
        found.append(ipaddress.IPv4Address(packed[12:16]))
    # ``::`` and ``::1`` also live in ::/96; they are handled by the
    # generic IPv6 path so the error message stays accurate.
    if addr in _IPV4_COMPATIBLE and not (addr.is_unspecified or addr.is_loopback):
        found.append(ipaddress.IPv4Address(packed[12:16]))

    return found


def _in_networks(
    addr: ipaddress._BaseAddress, networks: tuple[ipaddress._BaseNetwork, ...]
) -> bool:
    """``addr in network`` for same-version pairs only.

    ``IPv6Address in IPv4Network`` returns ``False`` rather than raising,
    which is exactly how the original predicate lost the mapped case.
    Comparing versions explicitly makes the intent visible and keeps the
    call site from depending on that quirk.
    """
    return any(addr.version == net.version and addr in net for net in networks)


def _v6_intrinsically_internal(addr: ipaddress.IPv6Address) -> bool:
    """Last-resort check for IPv6 addresses with no embedded IPv4.

    Only reached for addresses outside the transition prefixes, so the
    ``::ffff:0:0/96``-is-private quirk in CPython cannot cause a false
    positive here.
    """
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
    )


def _blocked_reason(
    addr: ipaddress._BaseAddress, allow_private: bool
) -> tuple[bool, str]:
    """Return ``(blocked, reason)`` for a single resolved address."""
    if addr.version == 4:
        if addr in _ALWAYS_BLOCKED_IPS:
            return True, f"cloud metadata IP {addr}"
        if not allow_private and _in_networks(addr, _PRIVATE_NETWORKS):
            return True, f"private/internal address {addr}"
        return False, ""

    embedded = _embedded_ipv4(addr)
    if embedded:
        # Judge the address that will actually be dialled, not the
        # wrapper. ``allow_private`` does not lift the metadata block.
        for v4 in embedded:
            if v4 in _ALWAYS_BLOCKED_IPS:
                return True, f"cloud metadata IP {v4} (IPv4-embedded in {addr})"
        if not allow_private:
            for v4 in embedded:
                if _in_networks(v4, _PRIVATE_NETWORKS):
                    return True, (
                        f"private/internal address {v4} (IPv4-embedded in {addr})"
                    )
        return False, ""

    if addr in _ALWAYS_BLOCKED_IPS:
        return True, f"cloud metadata IP {addr}"
    if not allow_private:
        if _in_networks(addr, _PRIVATE_NETWORKS):
            return True, f"private/internal address {addr}"
        if _v6_intrinsically_internal(addr):
            return True, f"private/internal address {addr}"
    return False, ""


def validate_url_target(url: str, allow_private: bool = False) -> tuple[bool, str]:
    """Validate a URL is safe to fetch. Returns (ok, error_message).

    Checks scheme, hostname, blocked hostnames, always-blocked IPs, and
    private network IPs (unless ``allow_private=True``).
    """
    _ensure_initialized()
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"Invalid URL: {e}"

    if parsed.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{parsed.scheme or 'none'}'"

    hostname = parsed.hostname
    if not hostname:
        return False, "Missing hostname or domain"

    hostname_lower = hostname.lower()
    if hostname_lower in _BLOCKED_HOSTNAMES:
        return False, f"Blocked: hostname {hostname} is a cloud metadata endpoint"

    try:
        ips: list[ipaddress._BaseAddress] = [ipaddress.ip_address(hostname)]
    except ValueError:
        ips = _resolve_hostname(hostname)
        if not ips:
            return False, f"Cannot resolve hostname: {hostname}"

    for ip in ips:
        blocked, reason = _blocked_reason(ip, allow_private)
        if blocked:
            return False, f"Blocked: {hostname} resolves to {reason}"

    return True, ""


def validate_resolved_url(url: str, allow_private: bool = False) -> tuple[bool, str]:
    """Validate an already-fetched URL (e.g. after a redirect).

    Skips DNS resolution for IP literal URLs. For domain hostnames,
    resolves and checks. Used for post-redirect SSRF protection in
    browser.py.
    """
    _ensure_initialized()
    try:
        parsed = urlparse(url)
    except Exception:
        return True, ""

    hostname = parsed.hostname
    if not hostname:
        return True, ""

    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        ips = _resolve_hostname(hostname)
        if not ips:
            return True, ""
        for ip in ips:
            blocked, reason = _blocked_reason(ip, allow_private)
            if blocked:
                return False, f"Blocked: redirect target {hostname} resolves to {reason}"
        return True, ""

    blocked, reason = _blocked_reason(addr, allow_private)
    if blocked:
        return False, f"Blocked: redirect target is {reason}"
    return True, ""


_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)


class SSRFRedirectError(Exception):
    """Raised when a request or one of its redirect hops is blocked."""


def ssrf_guard_transport(allow_private: bool = False, proxy: str | None = None):
    """Build an ``httpx`` transport that validates every hop *before* sending.

    Post-response validation is not enough: by the time
    ``validate_resolved_url`` runs on ``response.url``, the request has
    already been delivered to whatever the redirect pointed at. Against
    ``169.254.169.254`` that means the cloud metadata service has already
    answered, and the TCP connection to an internal host has already been
    made.

    Hooking ``handle_request`` means each hop — the original URL and every
    redirect target — is checked before a socket is opened, so a hostile
    redirect chain cannot reach the internal network at all.

    *proxy* is accepted here rather than mounted beside the guard, because
    httpx keys ``mounts`` by URL pattern: two entries registered for
    ``all://`` collide and the second silently replaces the first, which
    would disable the guard on any proxy-configured deployment.
    """
    import httpx

    class _GuardedTransport(httpx.AsyncHTTPTransport):
        """Validates every hop, then delegates to a (possibly proxied)
        transport.

        The proxy is held here rather than mounted alongside, because httpx
        keys ``mounts`` by URL pattern: registering the guard and the proxy
        under ``all://`` would collide and the second entry would silently
        replace the first, disabling the guard entirely on any deployment
        that configures a proxy.
        """

        def __init__(self, *args, proxy: str | None = None, **kw) -> None:
            self._allow_private = allow_private
            if proxy:
                kw["proxy"] = proxy
            super().__init__(*args, **kw)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            ok, err = validate_resolved_url(
                str(request.url), allow_private=self._allow_private
            )
            if not ok:
                raise SSRFRedirectError(err)
            return await super().handle_async_request(request)

    return _GuardedTransport(proxy=proxy)


def guarded_client(**kwargs) -> "httpx.AsyncClient":
    """An ``AsyncClient`` that refuses to connect to a blocked host.

    Combines the guard transport with ``follow_redirects=True`` so every
    hop is intercepted. A configured proxy is passed *into* the guard
    transport rather than mounted beside it, so the check runs whether or
    not a proxy is set.

    Returns the client; the caller owns its lifecycle.
    """
    import httpx

    allow_private = bool(kwargs.pop("allow_private", False))
    proxy = kwargs.pop("proxy", None)
    transport = ssrf_guard_transport(allow_private=allow_private, proxy=proxy)
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=True,
        **kwargs,
    )


def _allowed_by_whitelist(
    addr: ipaddress._BaseAddress, allowed_networks: list[ipaddress._BaseNetwork]
) -> bool:
    """Whitelist check that also honours IPv4 addresses encoded in IPv6.

    Without the embedded candidates, ``allowed_ips=["127.0.0.1"]`` would
    not match ``http://[::ffff:127.0.0.1]/`` and the operator's explicit
    exemption would look like it had no effect.
    """
    candidates: list[ipaddress._BaseAddress] = [addr]
    if isinstance(addr, ipaddress.IPv6Address):
        candidates.extend(_embedded_ipv4(addr))
    return any(_in_networks(c, tuple(allowed_networks)) for c in candidates)


def contains_internal_url(
    command: str,
    allowed_ips: list[str] | None = None,
    allow_private: bool = False,
) -> bool:
    """Return True if *command* contains a URL targeting a private/internal address.

    Args:
        command: The command string to scan.
        allowed_ips: Optional whitelist of IPs/CIDRs that bypass blocking.
        allow_private: If True, private network URLs are not flagged.
    """
    _ensure_initialized()
    allowed_networks: list[ipaddress._BaseNetwork] = []
    if allowed_ips:
        for raw in allowed_ips:
            s = (raw or "").strip()
            if not s:
                continue
            try:
                allowed_networks.append(ipaddress.ip_network(s, strict=False))
            except ValueError:
                try:
                    addr = ipaddress.ip_address(s)
                    allowed_networks.append(ipaddress.ip_network(str(addr), strict=False))
                except ValueError:
                    logger.error(
                        "SSRF: ignoring invalid allowed_internal_ips entry {!r}",
                        raw,
                    )

    for match in _URL_RE.finditer(command):
        url = match.group(0)
        ok, _ = validate_url_target(url, allow_private=allow_private)
        if not ok:
            try:
                parsed = urlparse(url)
                hostname = parsed.hostname
                if hostname:
                    addr = ipaddress.ip_address(hostname)
                    if _allowed_by_whitelist(addr, allowed_networks):
                        continue
            except ValueError:
                pass
            return True
    return False


__all__ = ["init_from_config", "validate_url_target", "validate_resolved_url", "contains_internal_url"]
