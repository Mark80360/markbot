"""Network validation utilities — SSRF protection and URL safety checks.

Self-contained IP/network validation with hardcoded private ranges.
The config-driven wrapper lives in :mod:`markbot.utils.ssrf`.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

# Networks that are always considered private/internal.
_BLOCKED_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),       # IPv4 loopback
    ipaddress.ip_network("10.0.0.0/8"),         # RFC 1918 private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC 1918 private
    ipaddress.ip_network("192.168.0.0/16"),     # RFC 1918 private
    ipaddress.ip_network("169.254.0.0/16"),     # IPv4 link-local / cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),          # "This host" network
    ipaddress.ip_network("::1/128"),             # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),            # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),           # IPv6 link-local
)


def _is_private(addr: ipaddress._BaseAddress) -> bool:
    """Return True if *addr* is a private/internal/loopback address."""
    for network in _BLOCKED_NETWORKS:
        if addr in network:
            return True
    # ipaddress built-in checks as a fallback (covers is_private, is_loopback,
    # is_link_local for both IPv4 and IPv6).
    if isinstance(addr, ipaddress.IPv4Address):
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified
    if isinstance(addr, ipaddress.IPv6Address):
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified
    return False


def _resolve_hostname(hostname: str) -> list[ipaddress._BaseAddress]:
    """Resolve *hostname* to IP addresses. Returns empty list on failure."""
    try:
        results = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return []

    ips: list[ipaddress._BaseAddress] = []
    seen: set[ipaddress._BaseAddress] = set()
    for _family, _type, _proto, _canon, sockaddr in results:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    return ips


def validate_url_target(url: str) -> tuple[bool, str]:
    """Validate that *url* is safe to fetch.

    Returns ``(True, "")`` if safe, otherwise ``(False, reason)``.
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"Invalid URL: {e}"

    if parsed.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{parsed.scheme or 'none'}'"

    hostname = parsed.hostname
    if not hostname:
        return False, "Missing hostname or domain"

    # If the hostname is already an IP literal, check it directly.
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        # Domain name — resolve via DNS.
        ips = _resolve_hostname(hostname)
        if not ips:
            return False, f"Cannot resolve hostname: {hostname}"
    else:
        ips = [addr]

    for ip in ips:
        if _is_private(ip):
            return False, f"Blocked: {hostname} resolves to private/internal address {ip}"

    return True, ""


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """Validate an already-resolved URL (e.g. after a redirect).

    For IP-literal hostnames the check is immediate; domain hostnames
    are resolved via DNS.  Absence of a hostname passes.
    """
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
            if _is_private(ip):
                return False, f"Blocked: redirect target {hostname} resolves to private address {ip}"
        return True, ""

    if _is_private(addr):
        return False, f"Blocked: redirect target is private address {addr}"
    return True, ""


_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)


def contains_internal_url(
    command: str,
    allowed_ips: list[str] | None = None,
) -> bool:
    """Return True if *command* contains a URL targeting a private address.

    Args:
        command: The command string to scan.
        allowed_ips: Optional whitelist of IPs/CIDRs that bypass blocking.
    """
    allowed_networks: list[ipaddress._BaseNetwork] = []
    if allowed_ips:
        for s in allowed_ips:
            try:
                allowed_networks.append(ipaddress.ip_network(s, strict=False))
            except ValueError:
                try:
                    addr = ipaddress.ip_address(s)
                    allowed_networks.append(
                        ipaddress.ip_network(str(addr), strict=False),
                    )
                except ValueError:
                    pass

    for match in _URL_RE.finditer(command):
        url = match.group(0)
        ok, _ = validate_url_target(url)
        if not ok:
            # Check if the URL's IP is whitelisted.
            try:
                parsed = urlparse(url)
                hostname = parsed.hostname
                if hostname:
                    addr = ipaddress.ip_address(hostname)
                    if any(addr in net for net in allowed_networks):
                        continue
            except ValueError:
                pass
            return True
    return False


__all__ = [
    "validate_url_target",
    "validate_resolved_url",
    "contains_internal_url",
]
