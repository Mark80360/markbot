"""Unit tests for the LLM response cache and the Anthropic
cache-control breakpoint strategy.

Covers:
- ``request_is_cacheable`` for greedy / non-stream / no-tools
- ``LLMResponseCache`` LRU and stats
- ``system_and_3`` breakpoint placement and TTL
"""

from __future__ import annotations

import unittest

from markbot.agent.anthropic_breakpoints import (
    attach_system_breakpoints,
    attach_tool_breakpoints,
    attach_user_breakpoint,
    make_cache_control,
    system_and_3,
)
from markbot.agent.llm_response_cache import (
    LLMResponseCache,
    request_is_cacheable,
)


class TestRequestIsCacheable(unittest.TestCase):
    def test_default(self) -> None:
        self.assertTrue(request_is_cacheable())

    def test_stream_excluded(self) -> None:
        self.assertFalse(request_is_cacheable(stream=True))

    def test_tools_excluded(self) -> None:
        self.assertFalse(request_is_cacheable(tools=[{"name": "x"}]))

    def test_tool_choice_required_excluded(self) -> None:
        self.assertFalse(request_is_cacheable(tool_choice="required"))

    def test_nonzero_temperature_excluded(self) -> None:
        self.assertFalse(request_is_cacheable(temperature=0.7))

    def test_zero_temperature_ok(self) -> None:
        self.assertTrue(request_is_cacheable(temperature=0.0))


class TestLLMResponseCache(unittest.TestCase):
    def test_miss_then_hit(self) -> None:
        cache = LLMResponseCache(capacity=8)
        self.assertIsNone(cache.get(b"k"))
        cache.put(b"k", {"content": "hi", "model": "m"})
        got = cache.get(b"k")
        self.assertIsNotNone(got)
        self.assertEqual(got.body["content"], "hi")
        self.assertEqual(cache.hits, 1)
        self.assertEqual(cache.misses, 1)
        self.assertEqual(cache.writes, 1)

    def test_lru_eviction(self) -> None:
        cache = LLMResponseCache(capacity=2)
        cache.put(b"a", {"v": 1})
        cache.put(b"b", {"v": 2})
        cache.put(b"c", {"v": 3})
        self.assertIsNone(cache.get(b"a"))
        self.assertIsNotNone(cache.get(b"b"))

    def test_invalidate(self) -> None:
        cache = LLMResponseCache()
        cache.put(b"x", {"v": 1})
        cache.invalidate(b"x")
        self.assertIsNone(cache.get(b"x"))

    def test_hit_count(self) -> None:
        cache = LLMResponseCache()
        cache.put(b"k", {"v": 1})
        for _ in range(3):
            cache.get(b"k")
        entry = cache.get(b"k")
        self.assertEqual(entry.hit_count, 4)  # 3 prior + this one


class TestAnthropicBreakpoints(unittest.TestCase):
    def test_make_cache_control(self) -> None:
        self.assertEqual(
            make_cache_control(),
            {"type": "ephemeral", "ttl": "5m"},
        )
        self.assertEqual(
            make_cache_control(ttl="1h"),
            {"type": "ephemeral", "ttl": "1h"},
        )

    def test_system_string_unchanged(self) -> None:
        # Plain-string system cannot accept cache_control.
        out = attach_system_breakpoints("hello")
        self.assertEqual(out, "hello")

    def test_system_list_marks_last(self) -> None:
        out = attach_system_breakpoints(
            [
                {"type": "text", "text": "A"},
                {"type": "text", "text": "B"},
            ]
        )
        self.assertNotIn("cache_control", out[0])
        self.assertIn("cache_control", out[-1])

    def test_tools_mark_trailing(self) -> None:
        out = attach_tool_breakpoints(
            [
                {"name": "a"},
                {"name": "b"},
                {"name": "c"},
                {"name": "d"},
            ],
            trailing=2,
        )
        # Only the last 2 carry a breakpoint.
        marked = [i for i, t in enumerate(out) if "cache_control" in t]
        self.assertEqual(marked, [2, 3])

    def test_user_tail_marks_last_text_block(self) -> None:
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": "second"},
        ]
        out = attach_user_breakpoint(msgs)
        last = out[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("cache_control", last["content"][-1])

    def test_system_and_3_combined(self) -> None:
        sys_blocks = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
        tools = [{"name": "a"}, {"name": "b"}]
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "bye"},
        ]
        out_sys, out_tools, out_msgs = system_and_3(
            system=sys_blocks, tools=tools, messages=msgs
        )
        # Last system block has a breakpoint.
        self.assertIn("cache_control", out_sys[-1])
        # Last tool has a breakpoint.
        self.assertIn("cache_control", out_tools[-1])
        # Last user message's content is now a list with breakpoint.
        last = out_msgs[-1]
        self.assertIsInstance(last["content"], list)
        self.assertIn("cache_control", last["content"][-1])

    def test_ttl_1h(self) -> None:
        out = attach_system_breakpoints(
            [{"type": "text", "text": "A"}], ttl="1h",
        )
        self.assertEqual(out[-1]["cache_control"]["ttl"], "1h")


if __name__ == "__main__":
    unittest.main()
