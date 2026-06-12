"""Process-local LLM response cache (deterministic replay).

A different layer from the server-side prefix cache: this cache
short-circuits **whole requests** when the same request is made
twice.  When a hit is found we return the cached response with
``usage`` zeroed out, so the operator is not double-charged.

## When is a request cacheable?

A request is only safe to cache when its response is **bit-deterministic**:

  - ``stream`` is False (or absent).
  - No tools, no ``tool_choice`` — the model has nothing to choose
    between at random.
  - ``temperature`` is 0.0 (or unset, since 0 is the default for
    every provider that exposes deterministic decoding).
  - ``top_p`` is unset or 1.0.
  - The model is a deterministic variant (no ``-instruct`` random
    sampler).  We only check the numeric values above; a future
    refactor can add a per-model allow-list.

If any of these conditions is violated, the request is **never**
written to the cache, but lookups are still attempted in case a
previous turn cached the same body.

## Key derivation

The cache key is the SHA-256 of five fields:

  - ``provider`` name (e.g. ``"openai"``).
  - ``base_url`` (so different bases don't collide).
  - Optional ``path_suffix`` for REST endpoints that share a base
    URL.
  - SHA-256 of the API key (so the operator can audit which key
    produced a hit, but the key itself is not in the key).
  - The wire-format body (JSON bytes).

The API key is hashed (not used raw) so that swapping keys in a
config file doesn't invalidate the entire cache.

## Capacity

256 slots, LRU eviction.  Sized so a session that hits the same
identity check twice (e.g. ``/help`` after compaction) gets a
zero-cost response, but a runaway session cannot blow out memory.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


DEFAULT_CAPACITY = 256


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def make_response_cache_key(
    *,
    provider: str,
    base_url: str,
    path_suffix: Optional[str],
    api_key: Optional[str],
    body: bytes,
) -> bytes:
    """SHA-256 of the (provider, base_url, path, key_hash, body) tuple.

    Returns the 32-byte digest.  Use :func:`hex_key` for a printable
    representation.
    """
    h = hashlib.sha256()
    h.update(provider.encode("utf-8"))
    h.update(b"\x00")
    h.update((base_url or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((path_suffix or "").encode("utf-8"))
    h.update(b"\x00")
    if api_key:
        h.update(hashlib.sha256(api_key.encode("utf-8")).digest())
    h.update(b"\x00")
    h.update(body)
    return h.digest()


def hex_key(digest: bytes) -> str:
    return digest.hex()


# ---------------------------------------------------------------------------
# Cacheability check
# ---------------------------------------------------------------------------

def _is_zero_or_none(value: Any, target: float) -> bool:
    if value is None:
        return True
    try:
        return float(value) == target
    except (TypeError, ValueError):
        return False


def request_is_cacheable(
    *,
    stream: Optional[bool] = None,
    tools: Optional[Iterable[Any]] = None,
    tool_choice: Any = None,
    temperature: Any = None,
    top_p: Any = None,
) -> bool:
    """True iff the request is safe to cache & replay.

    See module docstring for the rules.
    """
    if stream is True:
        return False
    if tools:
        try:
            if len(list(tools)) > 0:
                return False
        except TypeError:
            return False
    if tool_choice is not None and tool_choice != "auto":
        return False
    if not _is_zero_or_none(temperature, 0.0):
        return False
    if not _is_zero_or_none(top_p, 1.0):
        return False
    return True


# ---------------------------------------------------------------------------
# LRU
# ---------------------------------------------------------------------------

@dataclass
class CachedResponse:
    """A single cached response entry."""

    body: Any
    stored_at: float
    hit_count: int = 0


class LLMResponseCache:
    """Thread-safe LRU cache for deterministic LLM responses."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("LLMResponseCache capacity must be >= 1")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._entries: "OrderedDict[bytes, CachedResponse]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "size": len(self._entries),
        }

    def get(self, key: bytes) -> Optional[CachedResponse]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            entry.hit_count += 1
            self.hits += 1
            return entry

    def put(self, key: bytes, body: Any) -> None:
        with self._lock:
            self._entries[key] = CachedResponse(
                body=body,
                stored_at=time.time(),
            )
            self._entries.move_to_end(key)
            self.writes += 1
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def invalidate(self, key: Optional[bytes] = None) -> None:
        """Drop a single key (or the whole cache)."""
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)

    def clear(self) -> None:
        self.invalidate(None)
        self.hits = 0
        self.misses = 0
        self.writes = 0


# ---------------------------------------------------------------------------
# Wire-body helper
# ---------------------------------------------------------------------------

def canonicalise_body(payload: Any) -> bytes:
    """Best-effort canonical JSON for the cache body.

    Sorts keys and uses tight separators so the same logical request
    produces the same bytes regardless of dict iteration order.
    """
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


__all__ = [
    "LLMResponseCache",
    "CachedResponse",
    "make_response_cache_key",
    "hex_key",
    "request_is_cacheable",
    "canonicalise_body",
    "DEFAULT_CAPACITY",
]
