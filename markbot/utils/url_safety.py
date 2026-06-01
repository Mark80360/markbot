"""URL safety checks — blocks requests to private/internal network addresses.

Prevents SSRF (Server-Side Request Forgery) where a malicious prompt could
trick the agent into fetching internal resources like cloud metadata
endpoints (169.254.169.254), localhost services, or private network hosts.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("169.254.169.253"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("::ffff:169.254.169.254"),
    ipaddress.ip_address("::ffff:169.254.170.2"),
    ipaddress.ip_address("::ffff:169.254.169.253"),
})

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("198.18.0.0/15"),
]


def _resolve_hostname(hostname: str) -> list[ipaddress._BaseAddress]:
    """Resolve a hostname to its IP addresses."""
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        ips = set()
        for family, _type, _proto, _canon, sockaddr in results:
            raw = sockaddr[0]
            try:
                ip = ipaddress.ip_address(raw)
                ips.add(ip)
            except ValueError:
                continue
        return list(ips)
    except socket.gaierror:
        return []


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    for network in _PRIVATE_NETWORKS:
        if ip in network:
            return True
    return False


def check_url_safety(url: str, allow_private: bool = False) -> str | None:
    """Check if a URL is safe to access.

    Returns None if safe, or an error message string if blocked.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return f"Invalid URL: {url}"

    hostname = parsed.hostname
    if not hostname:
        return f"URL has no hostname: {url}"

    hostname_lower = hostname.lower()
    if hostname_lower in _BLOCKED_HOSTNAMES:
        return f"Blocked: hostname {hostname} is a cloud metadata endpoint"

    ips = _resolve_hostname(hostname)
    if not ips:
        return None

    for ip in ips:
        if ip in _ALWAYS_BLOCKED_IPS:
            return f"Blocked: {hostname} resolves to cloud metadata IP {ip}"

    if not allow_private:
        for ip in ips:
            if _is_private_ip(ip):
                return f"Blocked: {hostname} resolves to private IP {ip}"

    return None
