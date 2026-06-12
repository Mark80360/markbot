"""Integration tests for the cache layer.

These exercise the *connections* between modules that the unit
tests don't see end-to-end:

- The cost tracker accepts a :class:`CanonicalUsage` and produces
  the right model-bucket totals.
- The provider-name lookup in :mod:`markbot.agent.iteration` maps
  to the right normaliser (we re-use the contract of the existing
  test_cache_protocol and just verify the lookup doesn't regress).
- The cache-event shape the agent loop emits is consumed cleanly
  by the TUI chip renderer.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from markbot.agent.cache_chip import render_cache_hit_chip
from markbot.agent.cache_protocol import CacheEvent, CanonicalUsage
from markbot.agent.iteration import USAGE_NORMALISERS, normalise_usage
from markbot.agent.cost import CostTracker, ModelPricing, PricingTable


class TestCostTrackerAcceptsCanonicalUsage(unittest.TestCase):
    def test_uses_canonical_buckets(self) -> None:
        pricing = PricingTable(custom={"m": ModelPricing(
            input_per_1k=3.0,
            output_per_1k=15.0,
            cache_read_per_1k=0.3,
            cache_creation_per_1k=3.75,
        )})
        tracker = CostTracker(pricing=pricing)
        # 1000 input, 0 read, 1000 creation, 0 output → 3.0 + 3.75
        u = CanonicalUsage(
            input_tokens=1000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=1000,
        )
        cost = tracker.add_canonical_usage(u, model="m")
        self.assertAlmostEqual(cost, 3.0 + 3.75, places=4)
        # Repeat the call with cache_read; total should now
        # include 0.3 per 1k reads.
        u2 = CanonicalUsage(
            input_tokens=1000,
            output_tokens=0,
            cache_read_tokens=500,
            cache_creation_tokens=0,
        )
        tracker.add_canonical_usage(u2, model="m")
        mu = tracker.get_model_usage("m")
        self.assertEqual(mu.cache_read_input_tokens, 500)

    def test_last_turn_cache_savings(self) -> None:
        pricing = PricingTable(custom={"m": ModelPricing(
            input_per_1k=3.0,
            output_per_1k=15.0,
            cache_read_per_1k=0.3,
            cache_creation_per_1k=3.75,
        )})
        tracker = CostTracker(pricing=pricing)
        # 1000 cache_read tokens at 0.3 vs 3.0 → saved 2.7/1k
        # = $2.70
        u = CanonicalUsage(
            input_tokens=200,
            cache_read_tokens=1000,
        )
        tracker.add_canonical_usage(u, model="m")
        saved = tracker.last_turn_cache_savings()
        self.assertIsNotNone(saved)
        self.assertAlmostEqual(saved, 2.7, places=4)


class TestIterationNormaliserDispatch(unittest.TestCase):
    """The lookup must remain stable as new providers are added."""

    def test_anthropic_dispatch(self) -> None:
        u = normalise_usage("anthropic", {
            "input_tokens": 100,
            "cache_read_input_tokens": 60,
            "cache_creation_input_tokens": 20,
        })
        self.assertEqual(u.cache_read_tokens, 60)
        self.assertEqual(u.cache_creation_tokens, 20)
        self.assertEqual(u.cache_miss_tokens, 20)

    def test_openai_compat_dispatch(self) -> None:
        u = normalise_usage("deepseek", {
            "input_tokens": 100,
            "prompt_cache_hit_tokens": 70,
        })
        self.assertEqual(u.cache_read_tokens, 70)
        # ``prompt_cache_miss_tokens`` not provided — the synthetic
        # residual is 30.
        self.assertEqual(u.cache_miss_tokens, 30)

    def test_unknown_provider_falls_back_to_openai_compat(self) -> None:
        u = normalise_usage("mystery", {
            "prompt_cache_hit_tokens": 5,
        })
        # Falls back to OpenAI-compat semantics.
        self.assertEqual(u.cache_read_tokens, 5)


class TestChipRendererConsumesEvent(unittest.TestCase):
    def test_event_with_low_rate(self) -> None:
        ev = CacheEvent(
            description="drift",
            cache_read_tokens=10,
            cache_miss_tokens=90,
            stability_pct=42,
        )
        text = render_cache_hit_chip(ev)
        self.assertIn("10%", text.plain)

    def test_event_without_data(self) -> None:
        ev = CacheEvent(description="unknown")
        text = render_cache_hit_chip(ev)
        self.assertIn("unavailable", text.plain)


class TestLookupTableIsExhaustive(unittest.TestCase):
    """A guard against silent mis-routing when a new provider is
    added without a corresponding normaliser."""

    KNOWN_PROVIDERS = {
        "openai", "openai_compat", "deepseek", "codex",
        "openai_codex", "anthropic", "claude", "azure_openai",
    }

    def test_all_known_have_a_normaliser(self) -> None:
        for p in self.KNOWN_PROVIDERS:
            self.assertIn(p, USAGE_NORMALISERS, f"missing normaliser for {p}")


if __name__ == "__main__":
    unittest.main()
