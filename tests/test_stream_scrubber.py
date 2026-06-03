"""Tests for ``markbot.agent.stream_scrubber`` — the per-session
pool that wires :class:`StreamingContextScrubber` into the agent loop.

These tests guard against the regression where ``StreamingContextScrubber``
was instantiated in ``MemoryManager`` but never invoked on the streaming
path, causing ``<memory-context>`` spans to leak into the user UI.
"""

import pytest

from markbot.agent.stream_scrubber import ScrubberPool
from markbot.memory.fencing import (
    MEMORY_CONTEXT_CLOSE,
    MEMORY_CONTEXT_OPEN,
)


class TestScrubberPoolBasics:
    def test_empty_pool_has_no_session(self):
        pool = ScrubberPool()
        assert not pool.has_session("cli:direct")
        assert pool.feed("cli:direct", "hi") == "hi"
        assert pool.has_session("cli:direct")

    def test_passthrough_without_fence_tags(self):
        pool = ScrubberPool()
        assert pool.feed("cli:direct", "hello world") == "hello world"
        assert pool.flush("cli:direct") == ""

    def test_unknown_session_flush_is_safe(self):
        pool = ScrubberPool()
        assert pool.flush("never:seen") == ""


class TestScrubberPoolScrubs:
    def test_complete_span_is_removed(self):
        pool = ScrubberPool()
        span = f"before{MEMORY_CONTEXT_OPEN}secret payload{MEMORY_CONTEXT_CLOSE}after"
        assert pool.feed("cli:direct", span) == "beforeafter"
        assert pool.flush("cli:direct") == ""

    def test_split_across_chunks_still_scrubbed(self):
        pool = ScrubberPool()
        # Chunk 1 ends mid-tag, chunk 2 completes the open tag and payload.
        out1 = pool.feed("cli:direct", "pre<mem")
        out2 = pool.feed("cli:direct", "ory-context>secret")
        out3 = pool.feed("cli:direct", f"{MEMORY_CONTEXT_CLOSE}post")
        assert out1 + out2 + out3 == "prepost"
        assert pool.flush("cli:direct") == ""

    def test_flush_inside_unclosed_span_discards(self):
        pool = ScrubberPool()
        pool.feed("cli:direct", f"before{MEMORY_CONTEXT_OPEN}leaked")
        # Stream ends while still inside the span; the held payload
        # is the open tag itself plus content, all must be discarded.
        assert pool.flush("cli:direct") == ""


class TestScrubberPoolSessionIsolation:
    def test_two_sessions_have_independent_state(self):
        pool = ScrubberPool()
        # Session A is inside a span (should be hidden), session B has
        # clean text (should pass through).
        pool.feed("session:A", f"visible{MEMORY_CONTEXT_OPEN}hidden")
        assert pool.feed("session:B", "clean text") == "clean text"
        # Session A flushes nothing (still inside span).
        assert pool.flush("session:A") == ""
        # Session B has a clean flush.
        assert pool.flush("session:B") == ""

    def test_reset_clears_state_without_dropping_instance(self):
        pool = ScrubberPool()
        pool.feed("cli:direct", "x")
        assert pool.has_session("cli:direct")
        pool.reset("cli:direct")
        # Session still tracked, but buffer cleared.
        assert pool.has_session("cli:direct")
        assert pool.feed("cli:direct", "y") == "y"

    def test_clear_drops_all_sessions(self):
        pool = ScrubberPool()
        pool.feed("a", "x")
        pool.feed("b", "y")
        pool.clear()
        assert not pool.has_session("a")
        assert not pool.has_session("b")
        # Next feed lazily re-creates from a clean slate.
        assert pool.feed("a", "x") == "x"


class TestScrubberPoolSecurityRegression:
    """Regression: ``<memory-context>`` must NEVER appear in flushed text."""

    @pytest.mark.parametrize(
        "delta",
        [
            f"{MEMORY_CONTEXT_OPEN}api_key=sk-abc123{MEMORY_CONTEXT_CLOSE}",
            f"hi{MEMORY_CONTEXT_OPEN}user prefers dark mode{MEMORY_CONTEXT_CLOSE}bye",
            f"{MEMORY_CONTEXT_OPEN}private note about user{MEMORY_CONTEXT_CLOSE}",
        ],
    )
    def test_fence_tag_content_never_leaks(self, delta: str) -> None:
        pool = ScrubberPool()
        visible = pool.feed("cli:direct", delta)
        trailing = pool.flush("cli:direct")
        combined = visible + trailing
        assert MEMORY_CONTEXT_OPEN not in combined
        assert MEMORY_CONTEXT_CLOSE not in combined
        assert "api_key" not in combined
        assert "private note" not in combined
        assert "user prefers" not in combined
