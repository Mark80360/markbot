"""Deprecated alias for :mod:`markbot.utils.ssrf`.

This module used to hold a **second, independent copy** of the SSRF
predicate, with its own hardcoded block lists. Two copies meant two
places to get IPv6 normalisation wrong, and they had in fact drifted:
this one blocked ``::ffff:8.8.8.8`` (a legitimate public address) while
``ssrf`` waved ``::ffff:169.254.169.254`` straight through. Neither
behaviour was the intended one.

Everything now delegates to :mod:`markbot.utils.ssrf`, so there is
exactly one implementation and one set of rules (the config-driven
ones). Kept as a module rather than deleted so existing imports keep
working; new code should import :mod:`markbot.utils.ssrf` directly.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from markbot.utils import ssrf as _ssrf
from markbot.utils.ssrf import (  # noqa: F401  (re-exported for compatibility)
    contains_internal_url,
    init_from_config,
    validate_resolved_url,
    validate_url_target,
)

__all__ = [
    "validate_url_target",
    "validate_resolved_url",
    "contains_internal_url",
]


def _is_private(addr: ipaddress._BaseAddress) -> bool:
    """Return True when *addr* would be rejected as private/internal.

    Delegates to the single SSRF predicate, so this can no longer drift
    from what the actual guard enforces.
    """
    _ssrf._ensure_initialized()
    blocked, _reason = _ssrf._blocked_reason(addr, allow_private=False)
    return blocked


def __getattr__(name: str) -> Any:
    """Resolve ``_BLOCKED_NETWORKS`` lazily.

    Binding it at import time would freeze whatever the block list was
    before ``init_from_config`` ran — i.e. an empty tuple — and hand
    callers a stale snapshot.
    """
    if name == "_BLOCKED_NETWORKS":
        _ssrf._ensure_initialized()
        return _ssrf._PRIVATE_NETWORKS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
