"""Prefix-stability management for MarkBot's LLM calls.

Server-side KV prefix caches (DeepSeek, Anthropic, OpenAI Codex) only
hit when the **byte-exact prefix** of the request matches the prior
request.  Any drift in the system prompt or tool catalog invalidates
the cache for every token that follows.

This module gives the agent the discipline to detect and surface
prefix drift, and the tooling to avoid it where possible:

- :class:`PrefixFingerprint` — SHA-256 of the system prompt and tool
  catalog, plus a combined hash.
- :class:`ToolCatalogCache` — LRU cache that memoises the catalog
  serialisation so we don't pay the JSON cost on every turn.
- :class:`PrefixStabilityManager` — the state machine that pins the
  first fingerprint and emits a :class:`PrefixChange` on drift.

The companion piece, :func:`system_prompt_text`, is used everywhere
upstream to extract the "pure text" of the system prompt for hashing.

## Why a per-tool identity hash?

Tool definitions can be large (60+ tools × ~1 KB each ≈ 60 KB).  JSON
serialising the catalog on every turn is the second-largest CPU cost
in the request path (after the token estimate).  Caching by an
8-byte identity key derived from ``(name, description, strict,
input_schema)`` lets us skip the SHA-256 in the common case of an
unchanged catalog.

## Why the "ignore" list?

The :attr:`ToolCatalogCache.IGNORE_KEYS` set excludes fields that are
in the SDK's tool object but not in the wire format sent to the
provider.  Hashing them would create false-positive drift detections
the moment an SDK version bumps its internal representation.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrefixFingerprint:
    """SHA-256 triple: system, tools, combined.

    The :attr:`combined_sha256` is the *canonical* key for stability
    checks — comparing only ``system_sha256`` would miss the case
    where tools change in a way that keeps the system prompt bytes
    identical.
    """

    system_sha256: str
    tools_sha256: str
    combined_sha256: str

    @classmethod
    def compute(
        cls,
        system_text: str,
        tools: Optional[Iterable[dict[str, Any]]],
    ) -> "PrefixFingerprint":
        sys_hash = hashlib.sha256(system_text.encode("utf-8")).hexdigest()
        if tools is None:
            tools_hash = ""
        else:
            catalog = tool_catalog_digest(tools)
            tools_hash = hashlib.sha256(catalog.encode("utf-8")).hexdigest()
        combined_input = f"{sys_hash}\n{tools_hash}".encode("utf-8")
        combined = hashlib.sha256(combined_input).hexdigest()
        return cls(
            system_sha256=sys_hash,
            tools_sha256=tools_hash,
            combined_sha256=combined,
        )

    def short(self, n: int = 12) -> str:
        """Short hex prefix suitable for log lines and TUI labels."""
        return self.combined_sha256[:n]


# ---------------------------------------------------------------------------
# Tool catalog digest
# ---------------------------------------------------------------------------

#: Keys that exist on the in-memory tool object but are not serialised
#: to the provider's wire format.  Hashing them would cause false
#: positive drift whenever an SDK version bumps its internal schema.
TOOL_IGNORE_KEYS: frozenset[str] = frozenset({
    "allowed_callers",
    "defer_loading",
    "input_examples",
    "cache_control",
    "execution",
    "display_name",
})


def tool_catalog_digest(tools: Iterable[dict[str, Any]]) -> str:
    """Stable JSON serialisation of the tool catalog for hashing.

    Tools are sorted by name so reordering the tool list (e.g. the
    registry re-emits tools in a different order) does **not** change
    the digest.  :data:`TOOL_IGNORE_KEYS` are stripped so SDK-only
    metadata is not part of the prefix cache key.
    """
    serialised: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, dict):
            t = {k: v for k, v in tool.items() if k not in TOOL_IGNORE_KEYS}
        else:
            t = {"name": getattr(tool, "name", str(tool))}
        serialised.append(t)
    serialised.sort(key=lambda d: d.get("name") or d.get("function", {}).get("name") or "")
    return json.dumps(serialised, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def tool_identity_hash(tool: dict[str, Any]) -> int:
    """8-byte identity hash of a single tool, for LRU keying.

    Falls back to the tool's name when the input doesn't look like a
    dict (e.g. SDK objects that haven't been converted).  The hash is
    derived from ``name``, ``description``, ``strict``, and the
    ``input_schema`` — these are exactly the fields that *can* differ
    across catalog rebuilds.
    """
    name = tool.get("name") or ""
    fn = tool.get("function") or {}
    name = name or fn.get("name") or ""
    desc = tool.get("description") or fn.get("description") or ""
    strict = tool.get("strict") or fn.get("strict") or False
    schema = tool.get("parameters") or tool.get("input_schema") or fn.get("parameters") or {}

    h = hashlib.blake2b(digest_size=8)
    h.update(name.encode("utf-8"))
    h.update(b"\x00")
    h.update(desc.encode("utf-8"))
    h.update(b"\x00")
    h.update(b"\x01" if strict else b"\x00")
    h.update(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return int.from_bytes(h.digest(), "big", signed=False)


# ---------------------------------------------------------------------------
# Tool catalog LRU
# ---------------------------------------------------------------------------

@dataclass
class _CachedCatalog:
    identity: int
    digest: str
    sha256: str
    serialised: str


class ToolCatalogCache:
    """LRU memoisation of the tool catalog serialisation.

    On a 60-tool catalog, JSON serialisation costs ~100 µs and SHA-256
    costs another ~10 µs.  Without this cache those costs land on
    every single LLM call.  An 8-slot LRU is enough to cover the
    "tools grew / tools shrank / tools stayed" alternation that
    happens during a session.
    """

    DEFAULT_CAPACITY = 8

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("ToolCatalogCache capacity must be >= 1")
        self._capacity = capacity
        self._cache: OrderedDict[int, _CachedCatalog] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._cache)

    @property
    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "size": len(self._cache)}

    def get_or_compute(self, tools: list[dict[str, Any]]) -> str:
        """Return the SHA-256 hex of the catalog, computing once per
        identity.  Updates LRU position.
        """
        if not tools:
            return ""
        # Identity = XOR of per-tool hashes (cheap rolling identity).
        identity = 0
        for tool in tools:
            identity ^= tool_identity_hash(tool)
        cached = self._cache.get(identity)
        if cached is not None:
            self.hits += 1
            self._cache.move_to_end(identity)
            return cached.sha256
        # Miss: serialise + hash.
        self.misses += 1
        digest = tool_catalog_digest(tools)
        sha = hashlib.sha256(digest.encode("utf-8")).hexdigest()
        self._cache[identity] = _CachedCatalog(
            identity=identity,
            digest=digest,
            sha256=sha,
            serialised=digest,
        )
        self._cache.move_to_end(identity)
        # Evict.
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return sha

    def clear(self) -> None:
        self._cache.clear()
        self.hits = 0
        self.misses = 0


# ---------------------------------------------------------------------------
# Prefix drift
# ---------------------------------------------------------------------------

@dataclass
class PrefixChange:
    """A single observation: did the prefix drift, and where?

    Attributes:
        system_changed: True iff the system prompt bytes differ.
        tools_changed: True iff the tool catalog hash differs.
        previous: The pinned fingerprint at the time of observation.
        current: The freshly-computed fingerprint.
    """

    system_changed: bool
    tools_changed: bool
    previous: Optional[PrefixFingerprint]
    current: PrefixFingerprint

    @property
    def label(self) -> str:
        if self.system_changed and self.tools_changed:
            return "sys+tools"
        if self.system_changed:
            return "sys"
        if self.tools_changed:
            return "tools"
        return "prefix"

    def __str__(self) -> str:
        prev = self.previous.short() if self.previous else "unpinned"
        return (
            f"prefix drift: {self.label} changed "
            f"(frozen={prev}, current={self.current.short()})"
        )


# ---------------------------------------------------------------------------
# Stability manager
# ---------------------------------------------------------------------------

class PrefixStabilityManager:
    """Stateful checker for prefix drift.

    The first call pins the fingerprint; subsequent calls compare
    against the pinned baseline.  On drift we re-pin to the new
    baseline immediately — the policy is "accept the new world" rather
    than "stay pinned to the old, broken, baseline" — so the next call
    is stable again and the operator gets a single, clear signal.

    The :attr:`stability_ratio` property is the headline metric
    surfaced to the TUI / :code:`/status`.
    """

    def __init__(self, tool_catalog_cache: Optional[ToolCatalogCache] = None) -> None:
        self._pinned: Optional[PrefixFingerprint] = None
        self._current: Optional[PrefixFingerprint] = None
        self._last_change: Optional[PrefixChange] = None
        self._change_count = 0
        self._check_count = 0
        self._tool_catalog_cache = tool_catalog_cache or ToolCatalogCache()

    # ------------------------------------------------------------------
    # State properties
    # ------------------------------------------------------------------
    @property
    def pinned(self) -> Optional[PrefixFingerprint]:
        return self._pinned

    @property
    def last_change(self) -> Optional[PrefixChange]:
        return self._last_change

    @property
    def check_count(self) -> int:
        return self._check_count

    @property
    def change_count(self) -> int:
        return self._change_count

    @property
    def stability_ratio(self) -> float:
        """0.0..1.0 — fraction of recent turns that were stable."""
        if self._check_count == 0:
            return 1.0
        return 1.0 - (self._change_count / self._check_count)

    @property
    def tool_catalog_cache(self) -> ToolCatalogCache:
        return self._tool_catalog_cache

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def check_and_update(
        self,
        system_text: str,
        tools: Optional[list[dict[str, Any]]],
    ) -> tuple[bool, Optional[PrefixChange]]:
        """Check the current prefix and update internal state.

        Returns:
            (stable, change) where ``stable`` is True iff the prefix
            matches the pinned baseline (or the first call), and
            ``change`` is a :class:`PrefixChange` iff drift was
            observed (always re-pinned to the new baseline).
        """
        self._check_count += 1
        # Use the LRU to get the tools hash cheaply.
        if tools is None:
            tools_hash = ""
        else:
            tools_hash = self._tool_catalog_cache.get_or_compute(tools)
        system_hash = hashlib.sha256(system_text.encode("utf-8")).hexdigest()
        combined_input = f"{system_hash}\n{tools_hash}".encode("utf-8")
        combined = hashlib.sha256(combined_input).hexdigest()
        current = PrefixFingerprint(
            system_sha256=system_hash,
            tools_sha256=tools_hash,
            combined_sha256=combined,
        )
        self._current = current

        if self._pinned is None:
            # First call: pin, report stable.
            self._pinned = current
            return True, None

        if (
            current.system_sha256 == self._pinned.system_sha256
            and current.tools_sha256 == self._pinned.tools_sha256
        ):
            return True, None

        # Drift.
        self._change_count += 1
        change = PrefixChange(
            system_changed=(current.system_sha256 != self._pinned.system_sha256),
            tools_changed=(current.tools_sha256 != self._pinned.tools_sha256),
            previous=self._pinned,
            current=current,
        )
        self._last_change = change
        # Re-pin to the new baseline.
        self._pinned = current
        return False, change

    def reset(self) -> None:
        """Forget all state.  Used by ``/clear`` and session reset."""
        self._pinned = None
        self._current = None
        self._last_change = None
        self._change_count = 0
        self._check_count = 0
        self._tool_catalog_cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def system_prompt_text(messages: list[dict[str, Any]]) -> str:
    """Extract the system prompt text from a messages list.

    MarkBot stores the system prompt as the first ``role=="system"``
    message with a string content.  Returns the empty string when no
    system message is present, so the caller can hash unconditionally
    without a None check.
    """
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # Anthropic / OpenAI block format — concatenate text blocks.
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                return "\n".join(parts)
            return str(content or "")
    return ""


def short_hash(s: str, n: int = 12) -> str:
    """Display-friendly hash prefix (for TUI / log lines)."""
    return s[:n] if s else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PrefixFingerprint",
    "PrefixStabilityManager",
    "PrefixChange",
    "ToolCatalogCache",
    "TOOL_IGNORE_KEYS",
    "tool_catalog_digest",
    "tool_identity_hash",
    "system_prompt_text",
    "short_hash",
]
