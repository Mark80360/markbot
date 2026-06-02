"""SSRF (Server-Side Request Forgery) protection utilities.

Block lists are loaded from :class:`markbot.config.schema.SsrfConfig`
via :func:`init_from_config`. This module holds no hardcoded block
lists — all rules live in the Config schema so they can be overridden
per deployment.
"""

from __future__ import annotations

import ipaddress
import socket  # noqa: F401  (reserved for _resolve_hostname, Task 3)
from urllib.parse import urlparse  # noqa: F401  (reserved for validate_url_target, Task 3)

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


__all__ = ["init_from_config"]
