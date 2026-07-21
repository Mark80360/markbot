"""SSRF (Server-Side Request Forgery) protection utilities.

Block lists are loaded from :class:`markbot.config.schema.SsrfConfig`
via :func:`init_from_config`. This module holds no hardcoded block
lists — all rules live in the Config schema so they can be overridden
per deployment.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from markbot.config.schema import Config

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset()
_ALWAYS_BLOCKED_IPS: tuple[ipaddress._BaseAddress, ...] = ()
_PRIVATE_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = ()
_INITIALIZED: bool = False


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
    global _BLOCKED_HOSTNAMES, _ALWAYS_BLOCKED_IPS, _PRIVATE_NETWORKS, _INITIALIZED
    ssrf_cfg = config.ssrf
    _BLOCKED_HOSTNAMES = frozenset(s.lower() for s in ssrf_cfg.blocked_hostnames)
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
        if ip in _ALWAYS_BLOCKED_IPS:
            return False, f"Blocked: {hostname} resolves to cloud metadata IP {ip}"

    if not allow_private:
        for ip in ips:
            for network in _PRIVATE_NETWORKS:
                if ip in network:
                    return False, f"Blocked: {hostname} resolves to private/internal address {ip}"

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
            if ip in _ALWAYS_BLOCKED_IPS:
                return False, f"Blocked: redirect target {hostname} resolves to cloud metadata IP {ip}"
        if not allow_private:
            for ip in ips:
                for network in _PRIVATE_NETWORKS:
                    if ip in network:
                        return False, f"Blocked: redirect target {hostname} resolves to private address {ip}"
        return True, ""

    if addr in _ALWAYS_BLOCKED_IPS:
        return False, f"Blocked: redirect target is cloud metadata IP {addr}"
    if not allow_private:
        for network in _PRIVATE_NETWORKS:
            if addr in network:
                return False, f"Blocked: redirect target is private address {addr}"
    return True, ""


_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)


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
        for s in allowed_ips:
            try:
                allowed_networks.append(ipaddress.ip_network(s, strict=False))
            except ValueError:
                try:
                    addr = ipaddress.ip_address(s)
                    allowed_networks.append(ipaddress.ip_network(str(addr), strict=False))
                except ValueError:
                    pass

    for match in _URL_RE.finditer(command):
        url = match.group(0)
        ok, _ = validate_url_target(url, allow_private=allow_private)
        if not ok:
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


__all__ = ["init_from_config", "validate_url_target", "validate_resolved_url", "contains_internal_url"]
