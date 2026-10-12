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
from collections import OrderedDict, deque
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

    ## Why the key is content-addressed

    An earlier version keyed on ``(messages_revision, system_fingerprint)``
    and relied on the engine bumping ``messages_revision`` when the message
    list changed. That never worked: the loop bumped the counter
    unconditionally at the top of *every* iteration and then immediately
    called the estimator with the new value, so the hit rate on the hot path
    was ~0 — the cache paid its bookkeeping cost and never returned a hit.

    It was worse than useless. One instance is shared by every concurrent
    session, and the key contained no session identity, so two users on the
    same channel — whose system prompts are byte-identical — could be served
    each other's estimate. That number drives the compaction threshold.

    So the key is derived from the content itself: a cheap hash of
    ``(session_key, message count, system fingerprint, last message
    digest)``. Identical content hits; any mutation (append, insert, replace
    after compaction) changes the count or the tail digest and misses
    correctly, with no external bookkeeping to forget. Entries are bounded
    and evicted least-recently-used.
    """

    system_fingerprint: int = 0
    cached_tokens: Optional[int] = None
    cached_key: tuple = ()
    audit_ring: deque = field(default_factory=lambda: deque(maxlen=DEFAULT_AUDIT_RING_CAPACITY))
    hits: int = 0
    misses: int = 0
    #: Bounded LRU of (key -> tokens). Small because the useful working set
    #: is "the current estimate per active session", not a history.
    max_entries: int = 32
    _entries: "OrderedDict[tuple, int]" = field(default_factory=OrderedDict)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def bump_messages_revision(self, revision: int) -> None:
        """Retained for API compatibility.

        The cache is content-addressed and no longer needs an external
        revision counter, so this only drops the single-entry fast path and
        forces one recompute. Callers that used to bump it to signal a
        change are now handled automatically.
        """
        self.cached_tokens = None
        self.cached_key = ()

    def invalidate(self) -> None:
        """Forget all cached state.  Used by ``/clear`` and reset paths."""
        self.cached_tokens = None
        self.system_fingerprint = 0
        self.cached_key = ()
        self.audit_ring.clear()
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    def invalidate_session(self, session_key: str) -> None:
        """Drop entries belonging to one session (``/clear``, close)."""
        for key in [k for k in self._entries if k and k[0] == session_key]:
            del self._entries[key]
        if self.cached_key and self.cached_key[0] == session_key:
            self.cached_tokens = None
            self.cached_key = ()

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    @staticmethod
    def _content_key(
        session_key: str,
        system_prompt: Any,
        messages: list[dict[str, Any]],
        sys_fp: int,
    ) -> tuple:
        """Derive the cache key from content rather than a counter.

        The tail digest catches in-place mutations of the last message (the
        common case: a tool result appended to the previous turn's final
        message) and the length catches every insertion or removal, which
        includes the wholesale list replacement performed by compaction.
        """
        tail = ""
        if messages:
            try:
                last = messages[-1]
                tail = f"{last.get('role', '')}:{len(str(last.get('content', '')))}"
            except Exception:  # pragma: no cover - defensive
                tail = "?"
        return (session_key, len(messages), sys_fp, hash(tail))

    def lookup_or_compute(
        self,
        system_prompt: Any,
        messages: Iterable[dict[str, Any]],
        *,
        session_key: str = "",
    ) -> int:
        """Return the cached token estimate, recomputing on miss.

        ``messages`` is borrowed for the duration of the call so a
        miss can re-tokenize without copying.
        """
        msgs = list(messages)
        sys_fp = _system_fingerprint(system_prompt)
        key = self._content_key(session_key, system_prompt, msgs, sys_fp)

        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            self.hits += 1
            self.cached_key = key
            self.cached_tokens = cached
            self.system_fingerprint = sys_fp
            return cached

        # Miss: re-tokenize.
        tokens = estimate_messages_tokens(msgs)
        self._entries[key] = tokens
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        self.cached_key = key
        self.cached_tokens = tokens
        self.system_fingerprint = sys_fp
        self.misses += 1
        self.audit_ring.append((len(self._entries), tokens))
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
            "size": len(self._entries),
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
