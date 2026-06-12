"""Unit tests for the prefix-cache layer.

Covers:
- PrefixFingerprint construction
- ToolCatalogCache LRU behaviour
- PrefixStabilityManager pinned / change detection
- system_prompt_text extraction from a messages list
- The drift event shape (system-only / tools-only / both)
"""

from __future__ import annotations

import unittest

from markbot.agent.prefix_cache import (
    PrefixFingerprint,
    PrefixStabilityManager,
    ToolCatalogCache,
    system_prompt_text,
)


def _tools(*names: str) -> list[dict]:
    return [{"name": n, "description": f"tool {n}"} for n in names]


class TestToolCatalogCache(unittest.TestCase):
    def test_lru_eviction(self) -> None:
        cache = ToolCatalogCache(capacity=2)
        cache.get_or_compute(_tools("a"))
        cache.get_or_compute(_tools("b"))
        cache.get_or_compute(_tools("a"))  # hit, recency
        # After three insertions into a capacity-2 cache, the
        # LRU (here, "b") must have been evicted.
        self.assertEqual(len(cache), 2)
        self.assertGreaterEqual(cache.hits, 1)

    def test_idempotent(self) -> None:
        cache = ToolCatalogCache()
        h1 = cache.get_or_compute(_tools("a"))
        h2 = cache.get_or_compute(_tools("a"))
        self.assertEqual(h1, h2)
        self.assertGreater(cache.hits, 0)

    def test_none_is_singleton_empty(self) -> None:
        cache = ToolCatalogCache()
        h1 = cache.get_or_compute(None)
        h2 = cache.get_or_compute(None)
        self.assertEqual(h1, h2)
        self.assertEqual(h1, "")


class TestPrefixStabilityManager(unittest.TestCase):
    def test_first_call_pins(self) -> None:
        pm = PrefixStabilityManager()
        stable, change = pm.check_and_update("system", _tools("a"))
        self.assertTrue(stable)
        self.assertIsNone(change)
        self.assertEqual(pm.check_count, 1)
        self.assertEqual(pm.change_count, 0)

    def test_stable_keeps_pinned(self) -> None:
        pm = PrefixStabilityManager()
        pm.check_and_update("system", _tools("a"))
        stable, change = pm.check_and_update("system", _tools("a"))
        self.assertTrue(stable)
        self.assertIsNone(change)
        self.assertEqual(pm.change_count, 0)

    def test_system_drift(self) -> None:
        pm = PrefixStabilityManager()
        pm.check_and_update("system-A", _tools("a"))
        stable, change = pm.check_and_update("system-B", _tools("a"))
        self.assertFalse(stable)
        self.assertIsNotNone(change)
        self.assertTrue(change.system_changed)
        self.assertFalse(change.tools_changed)
        self.assertEqual(pm.change_count, 1)

    def test_tools_drift(self) -> None:
        pm = PrefixStabilityManager()
        pm.check_and_update("system", _tools("a"))
        stable, change = pm.check_and_update("system", _tools("a", "b"))
        self.assertFalse(stable)
        self.assertIsNotNone(change)
        self.assertFalse(change.system_changed)
        self.assertTrue(change.tools_changed)

    def test_both_drift(self) -> None:
        pm = PrefixStabilityManager()
        pm.check_and_update("A", _tools("a"))
        stable, change = pm.check_and_update("B", _tools("b"))
        self.assertFalse(stable)
        self.assertTrue(change.system_changed and change.tools_changed)

    def test_stability_ratio_decreases_on_drift(self) -> None:
        pm = PrefixStabilityManager()
        pm.check_and_update("A", _tools("a"))  # 1/1 stable
        pm.check_and_update("A", _tools("a"))  # 2/2 stable
        pm.check_and_update("B", _tools("a"))  # 2/3 stable
        self.assertAlmostEqual(pm.stability_ratio, 2 / 3, places=2)

    def test_fingerprint_short(self) -> None:
        fp = PrefixFingerprint("aa" * 32, "bb" * 32, "cc" * 32)
        self.assertEqual(len(fp.short()), 12)


class TestSystemPromptExtraction(unittest.TestCase):
    def test_string_system(self) -> None:
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
        ]
        self.assertEqual(system_prompt_text(msgs), "You are helpful.")

    def test_block_system(self) -> None:
        msgs = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "Block-1"},
                    {"type": "text", "text": "Block-2"},
                ],
            },
            {"role": "user", "content": "hi"},
        ]
        text = system_prompt_text(msgs)
        self.assertIn("Block-1", text)
        self.assertIn("Block-2", text)

    def test_no_system_message(self) -> None:
        self.assertEqual(system_prompt_text([{"role": "user", "content": "x"}]), "")


if __name__ == "__main__":
    unittest.main()
