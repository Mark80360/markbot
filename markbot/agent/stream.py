"""Stream filtering utilities for LLM output processing.

Extracted from agent/loop.py to isolate the think-tag stream filter.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    # upstream accepts sync (returns None) or async (returns Awaitable) callbacks
    UpstreamCallback = Callable[[str], Union[Awaitable[None], None]]


class StreamFilter:
    """Filters think tag blocks from streaming LLM output.

    Wraps an upstream callback so downstream consumers never see raw
    think blocks -- only incremental clean text deltas are forwarded.

    Supports both sync and async upstream callbacks.
    """

    def __init__(self, upstream: "UpstreamCallback | None" = None) -> None:
        self._upstream = upstream
        self._buf = ""

    async def __call__(self, delta: str) -> None:
        """Forward *delta* to upstream after stripping think blocks.

        Maintains an internal buffer to correctly compute the incremental
        clean text between successive deltas.
        """
        from markbot.utils.helpers import strip_think  # noqa: PLC0415

        prev_clean = strip_think(self._buf)
        self._buf += delta
        new_clean = strip_think(self._buf)
        incremental = new_clean[len(prev_clean):]
        if incremental and self._upstream:
            result = self._upstream(incremental)
            if asyncio.iscoroutine(result):
                await result

    def reset(self) -> None:
        """Clear internal buffer (call between turns)."""
        self._buf = ""

    @property
    def buffer(self) -> str:
        """Raw buffer content (with think blocks)."""
        return self._buf