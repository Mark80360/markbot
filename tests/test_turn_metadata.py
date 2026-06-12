"""Unit tests for turn-metadata injection and the cache chip renderer.

These cover:
- ``attach_turn_meta`` for both string and list-style content
- ``strip_turn_meta_block`` round-trip
- ``render_cache_hit_chip`` colour mapping
- The cache-discipline marker presence in the system prompt
"""

from __future__ import annotations

import unittest

from markbot.agent.cache_chip import render_cache_hit_chip
from markbot.agent.cache_discipline import (
    CACHE_DISCIPLINE_SECTION,
    VOLATILE_BOUNDARY_MARKER,
)
from markbot.agent.cache_protocol import CacheEvent
from markbot.agent.turn_metadata import (
    TurnMetadata,
    attach_turn_meta,
    make_turn_metadata,
    render_turn_meta_block,
    strip_turn_meta_block,
)


class TestTurnMetadata(unittest.TestCase):
    def test_render_block(self) -> None:
        meta = TurnMetadata(
            date="2026-06-12",
            ts=1718179200.0,
            model="claude-3-5-sonnet",
            working_set="a.py, b.py",
        )
        block = render_turn_meta_block(meta)
        self.assertIn("2026-06-12", block)
        self.assertIn("claude-3-5-sonnet", block)
        self.assertIn("a.py", block)

    def test_attach_to_string(self) -> None:
        meta = TurnMetadata(date="2026-06-12", ts=1718179200.0, model="m")
        out = attach_turn_meta("hello world", meta)
        self.assertIn("hello world", out)
        self.assertIn("2026-06-12", out)

    def test_attach_is_idempotent(self) -> None:
        meta = TurnMetadata(date="2026-06-12", ts=1718179200.0, model="m")
        once = attach_turn_meta("hi", meta)
        twice = attach_turn_meta(once, meta)
        # Second attach replaces the first block — there should
        # still be exactly one block in the result.
        self.assertEqual(twice.count("<turn_meta>"), 1)

    def test_strip_round_trip(self) -> None:
        meta = TurnMetadata(date="2026-06-12", ts=1718179200.0, model="m")
        attached = attach_turn_meta("hi there", meta)
        cleaned, found = strip_turn_meta_block(attached)
        self.assertTrue(found)
        self.assertIn("hi there", cleaned)
        self.assertNotIn("turn_meta", cleaned)

    def test_attach_to_list(self) -> None:
        meta = TurnMetadata(date="2026-06-12", ts=1718179200.0, model="m")
        content = [
            {"type": "text", "text": "hello"},
            {"type": "image", "source": {"type": "base64", "data": "x"}},
        ]
        out = attach_turn_meta(content, meta)
        self.assertEqual(len(out), 3)
        # The image block is preserved untouched.
        self.assertEqual(out[1]["type"], "image")
        # The new block is a text block.
        self.assertEqual(out[-1]["type"], "text")
        self.assertIn("2026-06-12", out[-1]["text"])

    def test_make_turn_metadata(self) -> None:
        meta = make_turn_metadata(model="claude")
        self.assertTrue(meta.date)
        self.assertEqual(meta.model, "claude")


class TestCacheDiscipline(unittest.TestCase):
    def test_section_includes_rules(self) -> None:
        for needle in (
            "Append, don't reorder",
            "/compact",
            "Cache: NN%",
        ):
            self.assertIn(needle, CACHE_DISCIPLINE_SECTION)

    def test_volatile_boundary_marker(self) -> None:
        self.assertIn("volatile boundary", VOLATILE_BOUNDARY_MARKER)
        self.assertIn("cache-friendly", VOLATILE_BOUNDARY_MARKER)


class TestCacheChip(unittest.TestCase):
    def test_chip_renders_text(self) -> None:
        ev = CacheEvent(
            description="stable",
            cache_read_tokens=80,
            cache_miss_tokens=20,
        )
        text = render_cache_hit_chip(ev)
        self.assertIn("80%", text.plain)

    def test_chip_unavailable(self) -> None:
        ev = CacheEvent(description="stable")
        text = render_cache_hit_chip(ev)
        self.assertIn("unavailable", text.plain)


if __name__ == "__main__":
    unittest.main()
