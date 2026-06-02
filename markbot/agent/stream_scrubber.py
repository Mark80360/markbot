"""Per-session streaming scrubber pool.

Wraps ``StreamingContextScrubber`` instances to keep ``<memory-context>``
fence tags out of the user-facing UI. Each session gets its own scrubber
state so concurrent or interleaved streams don't interfere.

Extracted from ``agent/loop.py`` so the session-management and
feed/flush logic can be tested without spinning up an ``AgentLoop``.
"""

from __future__ import annotations

from markbot.memory.fencing import StreamingContextScrubber


class ScrubberPool:
    """Pool of :class:`StreamingContextScrubber` keyed by session_key.

    A single ``ScrubberPool`` instance is owned by the agent loop. It
    lazily creates a scrubber for any session that publishes streamed
    text and exposes the same ``feed`` / ``flush`` semantics as
    ``StreamingContextScrubber`` so call sites remain trivial::

        pool = ScrubberPool()
        visible = pool.feed("cli:direct", "before<memory-context>secret")
        # -> "before"
        trailing = pool.flush("cli:direct")
        # -> "" (inside unclosed span => discarded)
    """

    def __init__(self) -> None:
        self._scrubbers: dict[str, StreamingContextScrubber] = {}

    def feed(self, session_key: str, delta: str) -> str:
        """Feed *delta* to the session's scrubber and return visible text.

        A new scrubber is created on first use. An empty string is
        returned when the delta was entirely consumed by fence tags or
        buffered as a partial tag fragment.
        """
        scrubber = self._get_or_create(session_key)
        return scrubber.feed(delta)

    def flush(self, session_key: str) -> str:
        """Flush held-back text at end-of-stream.

        Returns ``""`` if the stream ended inside an unclosed
        ``<memory-context>`` span (the buffered content is discarded).
        """
        scrubber = self._scrubbers.get(session_key)
        if scrubber is None:
            return ""
        return scrubber.flush()

    def reset(self, session_key: str) -> None:
        """Reset scrubber state for a new turn without dropping the instance."""
        scrubber = self._scrubbers.get(session_key)
        if scrubber is not None:
            scrubber.reset()

    def clear(self) -> None:
        """Drop all scrubbers. Call on agent shutdown to release memory."""
        self._scrubbers.clear()

    def has_session(self, session_key: str) -> bool:
        return session_key in self._scrubbers

    def _get_or_create(self, session_key: str) -> StreamingContextScrubber:
        scrubber = self._scrubbers.get(session_key)
        if scrubber is None:
            scrubber = StreamingContextScrubber()
            self._scrubbers[session_key] = scrubber
        return scrubber


__all__ = ["ScrubberPool"]
