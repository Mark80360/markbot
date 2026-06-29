"""Website access policy — user-managed domain blocklist.

Loads a blocklist from markbot config and enforces it for browser/web tools.
Policy is cached in memory with a short TTL so config changes take effect quickly.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30.0
_cache_lock = threading.Lock()
_cached_policy: Optional[dict[str, Any]] = None
_cached_policy_time: float = 0.0


class WebsitePolicyError(Exception):
    pass


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def _load_policy_from_config() -> dict[str, Any]:
    """Load website policy from markbot config."""
    try:
        from markbot.config.loader import load_config
        config = load_config()
        browser_cfg = getattr(config, "tools", None)
        if browser_cfg and hasattr(browser_cfg, "browser"):
            br = browser_cfg.browser
            return {
                "blocked_domains": getattr(br, "blocked_domains", []),
                "allowed_domains": getattr(br, "allowed_domains", []),
            }
    except Exception:
        logger.warning("Failed to load website policy from config; blocklist disabled", exc_info=True)
    return {"blocked_domains": [], "allowed_domains": []}


def _get_policy() -> dict[str, Any]:
    """Get cached policy, reloading if TTL expired."""
    global _cached_policy, _cached_policy_time

    now = time.time()
    with _cache_lock:
        if _cached_policy is not None and (now - _cached_policy_time) < _CACHE_TTL_SECONDS:
            return _cached_policy

        policy = _load_policy_from_config()
        _cached_policy = policy
        _cached_policy_time = now
        return policy


def clear_cache() -> None:
    """Clear the policy cache (e.g. after config change)."""
    global _cached_policy, _cached_policy_time
    with _cache_lock:
        _cached_policy = None
        _cached_policy_time = 0.0


def check_website_policy(url: str) -> str | None:
    """Check if a URL is allowed by the website policy.

    Returns None if allowed, or an error message string if blocked.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return f"Invalid URL: {url}"

    host = _normalize_host(parsed.hostname or "")
    if not host:
        return None

    policy = _get_policy()

    blocked = policy.get("blocked_domains", [])
    for pattern in blocked:
        if fnmatch.fnmatch(host, pattern.lower()):
            return f"Blocked by website policy: {host} matches {pattern}"

    allowed = policy.get("allowed_domains", [])
    if allowed:
        matched = any(fnmatch.fnmatch(host, pattern.lower()) for pattern in allowed)
        if not matched:
            return f"Blocked by website policy: {host} not in allowlist"

    return None
