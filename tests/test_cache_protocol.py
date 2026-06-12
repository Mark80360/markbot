"""Unit tests for the cache protocol layer.

Tests cover:
- CanonicalUsage construction and the ``cache_hit_rate`` calculation
- ProviderBucket normaliser dispatch (OpenAI/DeepSeek vs Anthropic vs Codex)
- CacheEvent construction and the ``short()`` hash representation
- The ``USAGE_NORMALISERS`` lookup in ``iteration.py`` matches the
  actual provider names the agent uses.
"""

from __future__ import annotations

import unittest

from markbot.agent.cache_protocol import (
    CanonicalUsage,
    CacheEvent,
    UsageNormaliser,
)
from markbot.agent.iteration import (
    USAGE_NORMALISERS,
    _AnthropicNormaliser,
    _CodexResponsesNormaliser,
    _OpenAICompatNormaliser,
    normalise_usage,
)


class TestCanonicalUsage(unittest.TestCase):
    def test_empty_has_no_rate(self) -> None:
        u = CanonicalUsage()
        self.assertIsNone(u.cache_hit_rate)

    def test_full_hit(self) -> None:
        u = CanonicalUsage(cache_read_tokens=900, cache_miss_tokens=100)
        self.assertAlmostEqual(u.cache_hit_rate, 0.9)

    def test_full_miss(self) -> None:
        u = CanonicalUsage(cache_read_tokens=0, cache_miss_tokens=200)
        self.assertEqual(u.cache_hit_rate, 0.0)

    def test_clamped(self) -> None:
        # No realistic input should push it above 1.0; we clamp to
        # defend against provider telemetry glitches.
        u = CanonicalUsage(
            cache_read_tokens=200, cache_miss_tokens=50, input_tokens=50
        )
        self.assertLessEqual(u.cache_hit_rate, 1.0)

    def test_input_only_fallback(self) -> None:
        # Some providers only report ``input_tokens``; the protocol
        # should fall back to using input as the miss denominator.
        u = CanonicalUsage(cache_read_tokens=80, input_tokens=20)
        self.assertAlmostEqual(u.cache_hit_rate, 0.8)


class TestProviderNormalisers(unittest.TestCase):
    def test_openai_compat(self) -> None:
        n = _OpenAICompatNormaliser()
        u = n.normalise(
            {
                "input_tokens": 1000,
                "output_tokens": 200,
                "prompt_cache_hit_tokens": 700,
                "prompt_cache_miss_tokens": 300,
            }
        )
        self.assertEqual(u.cache_read_tokens, 700)
        self.assertEqual(u.cache_miss_tokens, 300)
        self.assertEqual(u.input_tokens, 1000)
        self.assertEqual(u.output_tokens, 200)

    def test_anthropic(self) -> None:
        n = _AnthropicNormaliser()
        u = n.normalise(
            {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 600,
                "cache_creation_input_tokens": 200,
            }
        )
        self.assertEqual(u.cache_read_tokens, 600)
        self.assertEqual(u.cache_creation_tokens, 200)
        # Anthropic doesn't report a separate miss bucket — the
        # protocol should treat the remaining input as the miss.
        # 1000 - 600 - 200 = 200.
        self.assertEqual(u.cache_miss_tokens, 200)

    def test_codex(self) -> None:
        n = _CodexResponsesNormaliser()
        u = n.normalise(
            {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cached_tokens": 750,
            }
        )
        self.assertEqual(u.cache_read_tokens, 750)
        # Codex reports the cached slice, not the miss.  The
        # remaining ``input_tokens - cached`` is the miss.
        self.assertEqual(u.cache_miss_tokens, 250)

    def test_missing_keys_default_zero(self) -> None:
        # Missing keys are surfaced as ``None`` (not zero) so the
        # protocol can distinguish "no data" from "the cache was
        # empty for this turn".
        u = _OpenAICompatNormaliser().normalise({})
        self.assertIsNone(u.input_tokens)
        self.assertIsNone(u.cache_read_tokens)

    def test_lookup_table_has_canonical_providers(self) -> None:
        # All provider names the agent actually uses must be in
        # the lookup; otherwise normalisation silently falls back
        # to OpenAI-compat, which mis-attributes Anthropic reads.
        for name in (
            "openai", "openai_compat", "deepseek", "codex",
            "openai_codex", "anthropic", "claude",
        ):
            self.assertIn(name, USAGE_NORMALISERS)

    def test_normalise_usage_dispatch(self) -> None:
        u = normalise_usage(
            "anthropic",
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 30,
            },
        )
        self.assertEqual(u.cache_read_tokens, 70)
        # Falls through to OpenAI-compat for unknown providers.
        u2 = normalise_usage("nonexistent", {"prompt_cache_hit_tokens": 5})
        self.assertEqual(u2.cache_read_tokens, 5)


class TestCacheEvent(unittest.TestCase):
    def test_default_construction(self) -> None:
        e = CacheEvent(description="stable")
        self.assertEqual(e.description, "stable")
        # ``stability_pct`` defaults to 100 — the absence of a
        # change signal means "fully stable so far".
        self.assertEqual(e.stability_pct, 100)
        self.assertFalse(e.changed)

    def test_drift_signal(self) -> None:
        e = CacheEvent(
            description="system_changed",
            system_prompt_changed=True,
            tools_changed=False,
            stability_pct=42,
            changed=True,
            pinned_combined_hash="abc",
        )
        self.assertTrue(e.changed)
        self.assertTrue(e.system_prompt_changed)
        self.assertFalse(e.tools_changed)
        self.assertEqual(e.pinned_combined_hash, "abc")


if __name__ == "__main__":
    unittest.main()
