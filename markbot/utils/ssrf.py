"""SSRF (Server-Side Request Forgery) protection utilities.

Block lists are loaded from :class:`markbot.config.schema.SsrfConfig`
via :func:`init_from_config`. This module holds no hardcoded block
lists — all rules live in the Config schema so they can be overridden
per deployment.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from markbot.config.schema import Config

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset()
_ALWAYS_BLOCKED_IPS: tuple[ipaddress._BaseAddress, ...] = ()
_PRIVATE_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = ()


def _parse_ips(ip_strings: list[str]) -> tuple[ipaddress._BaseAddress, ...]:
    """Parse IP strings into ipaddress objects. Invalid entries are skipped."""
    out: list[ipaddress._BaseAddress] = []
    for s in ip_strings:
        try:
            out.append(ipaddress.ip_address(s))
        except ValueError:
            continue
    return tuple(out)


def _parse_networks(net_strings: list[str]) -> tuple[ipaddress._BaseNetwork, ...]:
    """Parse CIDR strings into ip_network objects. Invalid entries are skipped."""
    out: list[ipaddress._BaseNetwork] = []
    for s in net_strings:
        try:
            out.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            continue
    return tuple(out)


def init_from_config(config: Config) -> None:
    """Populate SSRF block lists from Config.ssrf section."""
    global _BLOCKED_HOSTNAMES, _ALWAYS_BLOCKED_IPS, _PRIVATE_NETWORKS
    ssrf_cfg = config.ssrf
    _BLOCKED_HOSTNAMES = frozenset(s.lower() for s in ssrf_cfg.blocked_hostnames)
    _ALWAYS_BLOCKED_IPS = _parse_ips(ssrf_cfg.always_blocked_ips)
    _PRIVATE_NETWORKS = _parse_networks(ssrf_cfg.blocked_networks)


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


def validate_url_target(url: str, allow_private: bool = False) -> tuple[bool, str]:
    """Validate a URL is safe to fetch. Returns (ok, error_message).

    Checks scheme, hostname, blocked hostnames, always-blocked IPs, and
    private network IPs (unless ``allow_private=True``).
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

    hostname_lower = hostname.lower()
    if hostname_lower in _BLOCKED_HOSTNAMES:
        return False, f"Blocked: hostname {hostname} is a cloud metadata endpoint"

    ips = _resolve_hostname(hostname)
    if not ips:
        return False, f"Cannot resolve hostname: {hostname}"

    for ip in ips:
        if ip in _ALWAYS_BLOCKED_IPS:
            return False, f"Blocked: {hostname} resolves to cloud metadata IP {ip}"

    if not allow_private:
        for ip in ips:
            for network in _PRIVATE_NETWORKS:
                if ip in network:
                    return False, f"Blocked: {hostname} resolves to private/internal address {ip}"

    return True, ""


__all__ = ["init_from_config", "validate_url_target"]
