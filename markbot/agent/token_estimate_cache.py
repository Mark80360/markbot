"""Process-local memoisation of the input-token estimate.

The conservative token estimator (see
:func:`markbot.agent.tokens.estimate_messages_tokens`) walks the
full message history and the active system prompt on every call.
This is the single hottest CPU cost in the agent loop, and it is
invoked from at least five sites per turn:

  - capacity pre-checkpoint / post-checkpoint
  - reactive compaction trigger
  - the seam manager / context-window guard
  - the ``/status`` and ``/debug`` slash commands
  - the context inspector (TUI panel)

A 200-message history with 5 KB of tool results costs roughly
2 ms per call — ~20 ms of pure waste per turn without memoisation.

## Cache key

The estimate is a pure function of
``(messages, system_prompt)``.  We use two cheap signals as the
cache key:

  - ``messages_revision`` — a monotonic counter that the engine
    bumps on every add / remove / clear of the message list.
  - ``system_fingerprint`` — a 64-bit hash of the system prompt
    text, computed on every lookup (cheap; ``DefaultHasher`` is
    O(n) with a small constant).

When both match the previously stored value, we return the cached
estimate.  When either changes (or the cache is empty), we run
the estimator, store the result, and bump the audit ring.

## Audit ring

A 64-entry ring of ``(revision, tokens)`` pairs gives the
observability layer a quick view of "how did the token estimate
evolve over the last 64 turns".  The ring is bounded so a
long-running session cannot blow out memory.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Optional

from markbot.agent.tokens import estimate_messages_tokens
# ``estimate_messages_tokens`` only takes the messages list; the
# system prompt is implicit in the ``system`` message at index 0.
# We still keep the system fingerprint as a *cache key component* —
# if the system prompt changes between revisions, the fingerprint
# changes, and the next lookup correctly invalidates even if the
# caller forgot to bump the revision.

#: Default ring capacity.  Sized so a 64-entry window covers a full
#: capacity controller observation cycle without unbounded growth.
DEFAULT_AUDIT_RING_CAPACITY = 64


# ---------------------------------------------------------------------------
# System-prompt fingerprint
# ---------------------------------------------------------------------------

def _system_fingerprint(system: Any) -> int:
    """64-bit hash of the system prompt's textual content.

    Accepts the same shapes MarkBot passes around:

      - ``None``
      - ``str``
      - A list of content blocks (Anthropic / OpenAI multimodal).
    """
    if system is None:
        return 0
    if isinstance(system, str):
        text = system
    elif isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        text = "\n".join(parts)
    else:
        text = str(system)
    # FNV-1a 64-bit.  Cheap and stable.
    h = 1469598103934665603
    for ch in text:
        h ^= ord(ch)
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@dataclass
class TokenEstimateCache:
    """Memoised input-token estimator.

    Lifetime: process-local only.  Cross-session persistence is out
    of scope (the cross-session prompt-base disk cache lives in
    :mod:`markbot.agent.prompt_persist`).
    """

    messages_revision: int = 0
    system_fingerprint: int = 0
    cached_tokens: Optional[int] = None
    audit_ring: deque = field(default_factory=lambda: deque(maxlen=DEFAULT_AUDIT_RING_CAPACITY))
    hits: int = 0
    misses: int = 0

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def bump_messages_revision(self, revision: int) -> None:
        """Record a messages-revision bump.

        Calling this with a value smaller than the current value is
        a no-op (the cache is monotonic).
        """
        if revision > self.messages_revision:
            self.messages_revision = revision
            # Force the next lookup to recompute.
            self.cached_tokens = None

    def invalidate(self) -> None:
        """Forget all cached state.  Used by ``/clear`` and reset paths."""
        self.cached_tokens = None
        self.system_fingerprint = 0
        self.audit_ring.clear()
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def lookup_or_compute(
        self,
        messages_revision: int,
        system_prompt: Any,
        messages: Iterable[dict[str, Any]],
    ) -> int:
        """Return the cached token estimate, recomputing on miss.

        ``messages`` is borrowed for the duration of the call so a
        miss can re-tokenize without copying.
        """
        sys_fp = _system_fingerprint(system_prompt)
        if (
            self.messages_revision == messages_revision
            and self.system_fingerprint == sys_fp
            and self.cached_tokens is not None
        ):
            self.hits += 1
            return self.cached_tokens

        # Miss: re-tokenize.  Convert the iterable to a list so the
        # downstream function can index it.
        msgs = list(messages)
        tokens = estimate_messages_tokens(msgs)
        self.messages_revision = messages_revision
        self.system_fingerprint = sys_fp
        self.cached_tokens = tokens
        self.misses += 1
        self.audit_ring.append((messages_revision, tokens))
        return tokens

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict[str, int | float]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "size": len(self.audit_ring),
            "hit_rate": (self.hits / total) if total else 0.0,
        }

    def last_tokens(self) -> Optional[int]:
        return self.cached_tokens


# ---------------------------------------------------------------------------
# Module-level singleton (for callers that don't need a per-loop instance)
# ---------------------------------------------------------------------------

_global_cache: Optional[TokenEstimateCache] = None


def get_global_cache() -> TokenEstimateCache:
    global _global_cache
    if _global_cache is None:
        _global_cache = TokenEstimateCache()
    return _global_cache


__all__ = [
    "TokenEstimateCache",
    "DEFAULT_AUDIT_RING_CAPACITY",
    "get_global_cache",
]
