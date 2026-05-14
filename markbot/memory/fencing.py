"""Context fencing — memory-context tags and streaming scrubber.

Provides utilities to wrap injected memory in fence tags and scrub
those tags from streaming LLM responses so memory context does not
leak to the user UI.

Usage:
    from markbot.memory.fencing import fence_context, StreamingContextScrubber

    # Wrap memory context before injecting into system prompt
    system_prompt += fence_context("Recalled facts: ...")

    # Scrub streaming responses to remove fence tags
    scrubber = StreamingContextScrubber()
    for delta in stream:
        clean = scrubber.feed(delta)
        if clean:
            emit(clean)
    trailing = scrubber.flush()
"""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MEMORY_CONTEXT_OPEN = "<memory-context>"
MEMORY_CONTEXT_CLOSE = "</memory-context>"

_SYSTEM_NOTE_RE = re.compile(
    r"\[System note:\s*The following is recalled memory context,\s*"
    r"NOT new user input\.\s*"
    r"Treat as (?:informational background data|authoritative reference data[^\]]*)\.\]\s*",
    re.IGNORECASE,
)

_FENCE_TAG_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r"<\s*memory-context\s*>[\s\S]*?<\s*/\s*memory-context\s*>",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Fencing helpers
# ---------------------------------------------------------------------------


def fence_context(content: str, system_note: bool = True) -> str:
    """Wrap memory context in fence tags for injection into system prompt.

    Args:
        content: The memory context content to wrap.
        system_note: If True, prepend a system note explaining that this is
            recalled memory, not new user input.

    Returns:
        Fence-tagged string ready for system prompt injection.
    """
    if not content:
        return ""

    note = (
        "[System note: The following is recalled memory context, "
        "NOT new user input. Treat as authoritative reference data.]\n"
    ) if system_note else ""

    return f"{MEMORY_CONTEXT_OPEN}\n{note}{content}\n{MEMORY_CONTEXT_CLOSE}\n"


def sanitize_context(text: str) -> str:
    """Strip fence tags, injected context blocks, and system notes from text.

    One-shot cleanup for non-streaming use cases (e.g. log scrubbing,
    post-processing full responses). For streaming, use
    ``StreamingContextScrubber`` instead.

    Args:
        text: Text that may contain fence-tagged memory context.

    Returns:
        Clean text with all memory context markers removed.
    """
    text = _INTERNAL_CONTEXT_RE.sub("", text)
    text = _SYSTEM_NOTE_RE.sub("", text)
    text = _FENCE_TAG_RE.sub("", text)
    return text


def is_fenced(text: str) -> bool:
    """Check if text contains memory-context fence tags."""
    return bool(_FENCE_TAG_RE.search(text))


# ---------------------------------------------------------------------------
# Streaming scrubber
# ---------------------------------------------------------------------------


class StreamingContextScrubber:
    """Stateful scrubber for streaming text that may contain split
    memory-context spans.

    Ported from the StreamingContextScrubber pattern. The one-shot
    ``sanitize_context`` regex cannot survive chunk boundaries: a
    ``<memory-context>`` opened in one delta and closed in a later delta
    leaks its payload to the UI because the non-greedy block regex needs
    both tags in one string. This scrubber runs a small state machine
    across deltas, holding back partial-tag tails and discarding
    everything inside a span (including the system-note line).

    Usage::

        scrubber = StreamingContextScrubber()
        for delta in stream:
            visible = scrubber.feed(delta)
            if visible:
                emit(visible)
        trailing = scrubber.flush()

    The scrubber is re-entrant. Call ``reset()`` between turns.
    """

    _OPEN_TAG = MEMORY_CONTEXT_OPEN.lower()
    _CLOSE_TAG = MEMORY_CONTEXT_CLOSE.lower()

    def __init__(self) -> None:
        self._in_span: bool = False
        self._buf: str = ""

    def reset(self) -> None:
        """Reset internal state for a new turn."""
        self._in_span = False
        self._buf = ""

    def feed(self, text: str) -> str:
        """Return the visible portion of *text* after scrubbing.

        Any trailing fragment that could be the start of an open/close tag
        is held back in the internal buffer and surfaced on the next
        ``feed()`` call or discarded/emitted by ``flush()``.

        Args:
            text: A chunk of streaming response text.

        Returns:
            Clean text with fence-tagged spans removed.
        """
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_span:
                idx = buf.lower().find(self._CLOSE_TAG)
                if idx == -1:
                    # Hold back a potential partial close tag; drop the rest
                    held = self._max_partial_suffix(buf, self._CLOSE_TAG)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                # Found close 鈥?skip span content + tag, continue
                buf = buf[idx + len(self._CLOSE_TAG):]
                self._in_span = False
            else:
                idx = buf.lower().find(self._OPEN_TAG)
                if idx == -1:
                    # No open tag 鈥?hold back a potential partial open tag
                    held = self._max_partial_suffix(buf, self._OPEN_TAG)
                    if held:
                        out.append(buf[:-held])
                        self._buf = buf[-held:]
                    else:
                        out.append(buf)
                    return "".join(out)
                # Emit text before the tag, enter span
                if idx > 0:
                    out.append(buf[:idx])
                buf = buf[idx + len(self._OPEN_TAG):]
                self._in_span = True

        return "".join(out)

    def flush(self) -> str:
        """Flush any remaining buffered text.

        Call after the stream ends. If we were inside a memory-context span
        that never closed, the remaining buffer is discarded.

        Returns:
            Remaining visible text, or empty string if we were inside a span.
        """
        if self._in_span:
            self._buf = ""
            return ""
        result = self._buf
        self._buf = ""
        return result

    @staticmethod
    def _max_partial_suffix(text: str, tag: str) -> int:
        """Return the longest suffix of *text* that is a prefix of *tag*.

        This detects partial tag fragments at the end of a chunk so they
        can be held back until the next chunk arrives.
        """
        lower = text.lower()
        for length in range(len(tag) - 1, 0, -1):
            if lower.endswith(tag[:length]):
                return length
        return 0


__all__ = [
    "fence_context",
    "sanitize_context",
    "is_fenced",
    "StreamingContextScrubber",
    "MEMORY_CONTEXT_OPEN",
    "MEMORY_CONTEXT_CLOSE",
]
