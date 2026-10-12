"""Tests for cost tracking and budget control."""

import pytest

from markbot.agent.cost import CostTracker, ModelPricing, ModelUsage, PricingTable
from markbot.types.exceptions import BudgetExceededError


class TestModelUsage:
    def test_total_tokens_sums_all_fields(self):
        usage = ModelUsage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=30,
            cache_read_input_tokens=20,
        )
        assert usage.total_tokens == 200

    def test_default_values_are_zero(self):
        usage = ModelUsage()
        assert usage.total_tokens == 0
        assert usage.api_calls == 0
        assert usage.cost_usd == 0.0


class TestCostTracker:
    def test_add_usage_tracks_tokens(self):
        tracker = CostTracker()
        tracker.add_usage(model="claude-sonnet-4-5", input_tokens=1000, output_tokens=500)
        usage = tracker.state.model_usage["claude-sonnet-4-5"]
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        assert tracker.total_cost > 0

    def test_add_usage_increments_api_calls(self):
        tracker = CostTracker()
        tracker.add_usage(model="gpt-4o", input_tokens=100, output_tokens=50)
        tracker.add_usage(model="gpt-4o", input_tokens=200, output_tokens=100)
        assert tracker.state.total_api_calls == 2

    def test_budget_exceeded_raises(self):
        tracker = CostTracker(max_budget_usd=0.001)
        with pytest.raises(BudgetExceededError):
            tracker.add_usage(model="claude-opus-4-5", input_tokens=10000, output_tokens=5000)

    def test_is_over_budget_after_exceeded(self):
        tracker = CostTracker(max_budget_usd=0.001)
        assert tracker.is_over_budget() is False
        try:
            tracker.add_usage(model="claude-sonnet-4-5", input_tokens=100000, output_tokens=50000)
        except BudgetExceededError:
            pass
        assert tracker.is_over_budget() is True

    def test_no_budget_limit_when_none(self):
        tracker = CostTracker(max_budget_usd=None)
        tracker.add_usage(model="claude-opus-4-5", input_tokens=100000, output_tokens=50000)
        assert tracker.is_over_budget() is False

    def test_warn_threshold_emits_warning(self, capfd):
        tracker = CostTracker(max_budget_usd=10.0, warn_threshold_usd=0.001)
        tracker.add_usage(model="claude-sonnet-4-5", input_tokens=10000, output_tokens=5000)
        assert tracker.state.total_cost_usd >= tracker.warn_threshold_usd

    def test_per_model_tracking(self):
        tracker = CostTracker()
        tracker.add_usage(model="claude-sonnet-4-5", input_tokens=1000, output_tokens=500)
        tracker.add_usage(model="gpt-4o", input_tokens=2000, output_tokens=1000)
        assert "claude-sonnet-4-5" in tracker.state.model_usage
        assert "gpt-4o" in tracker.state.model_usage
        assert tracker.state.model_usage["claude-sonnet-4-5"].input_tokens == 1000
        assert tracker.state.model_usage["gpt-4o"].input_tokens == 2000

    def test_cache_tokens_tracked(self):
        tracker = CostTracker()
        tracker.add_usage(
            model="claude-sonnet-4-5",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=100,
        )
        usage = tracker.state.model_usage["claude-sonnet-4-5"]
        assert usage.cache_creation_input_tokens == 200
        assert usage.cache_read_input_tokens == 100

    def test_unknown_model_uses_default_pricing(self):
        tracker = CostTracker()
        tracker.add_usage(model="unknown-model-xyz", input_tokens=1000, output_tokens=500)
        assert tracker.total_cost > 0

    def test_custom_pricing(self):
        custom_pricing = PricingTable(custom={"my-model": ModelPricing(input_per_1k=0.01, output_per_1k=0.02)})
        tracker = CostTracker(pricing=custom_pricing)
        tracker.add_usage(model="my-model", input_tokens=1000, output_tokens=1000)
        expected = 0.01 + 0.02
        assert abs(tracker.total_cost - expected) < 0.001

    def test_total_cost_property(self):
        tracker = CostTracker()
        tracker.add_usage(model="gpt-4o", input_tokens=1000, output_tokens=500)
        assert isinstance(tracker.total_cost, float)
        assert tracker.total_cost > 0


class TestUpdateFromResponse:
    """``update_from_response`` is the subagent loop's only accounting path,
    so it must understand every provider's cache bucket names — not just
    Anthropic's four flat keys."""

    def test_anthropic_shape(self):
        tracker = CostTracker()
        class R:
            usage = {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_creation_input_tokens": 300,
                "cache_read_input_tokens": 50,
            }
        tracker.update_from_response(R(), model="claude-sonnet-4-5")
        u = tracker.state.model_usage["claude-sonnet-4-5"]
        assert (u.input_tokens, u.output_tokens) == (1000, 200)
        assert u.cache_creation_input_tokens == 300
        assert u.cache_read_input_tokens == 50

    def test_deepseek_cache_hit_bucket(self):
        tracker = CostTracker()
        class R:
            usage = {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_cache_hit_tokens": 800,
            }
        tracker.update_from_response(R(), model="deepseek-chat")
        u = tracker.state.model_usage["deepseek-chat"]
        # prompt_tokens is the TOTAL prompt; 800 of them were cache hits, so
        # only the 200-token residual is billed at the full input rate.
        assert u.input_tokens == 200
        assert u.output_tokens == 100
        assert u.cache_read_input_tokens == 800

    def test_openai_nested_cached_tokens(self):
        tracker = CostTracker()
        class R:
            usage = {
                "prompt_tokens": 2000,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 1500},
            }
        tracker.update_from_response(R(), model="gpt-4o")
        u = tracker.state.model_usage["gpt-4o"]
        assert u.input_tokens == 500
        assert u.cache_read_input_tokens == 1500

    def test_codex_cached_input_tokens(self):
        tracker = CostTracker()
        class R:
            usage = {"input_tokens": 900, "output_tokens": 20, "cached_input_tokens": 700}
        tracker.update_from_response(R(), model="gpt-5-codex")
        u = tracker.state.model_usage["gpt-5-codex"]
        assert u.cache_read_input_tokens == 700

    def test_no_usage_is_free(self):
        tracker = CostTracker()
        class R:
            usage = None
        assert tracker.update_from_response(R(), model="gpt-4o") == 0.0
        assert tracker.state.total_api_calls == 0

    def test_cached_call_costs_less_than_uncached(self):
        """A cache hit must actually reduce the billed cost, otherwise the
        cache-read bucket is being counted but never priced. OpenAI shape:
        ``prompt_tokens`` is the total prompt, cache hits live inside it."""
        pricing = PricingTable(custom={
            "m": ModelPricing(
                input_per_1k=1.0, output_per_1k=1.0,
                cache_read_per_1k=0.01, cache_creation_per_1k=1.0,
            )
        })
        hot = CostTracker(pricing=pricing)
        cold = CostTracker(pricing=pricing)
        # 1000 total prompt tokens, all served from cache -> $0.01.
        hot.update_from_response(
            type("R", (), {"usage": {"prompt_tokens": 1000, "completion_tokens": 0,
                                     "prompt_tokens_details": {"cached_tokens": 1000}}})(),
            model="m")
        # Same total, nothing cached -> $1.00.
        cold.update_from_response(
            type("R", (), {"usage": {"prompt_tokens": 1000, "completion_tokens": 0}})(),
            model="m")
        assert abs(hot.total_cost - 0.01) < 1e-6
        assert hot.total_cost < cold.total_cost / 10

    def test_anthropic_input_tokens_not_reduced(self):
        """Anthropic reports ``input_tokens`` as already-uncached, so the
        cache buckets must be added, not subtracted."""
        pricing = PricingTable(custom={
            "m": ModelPricing(
                input_per_1k=1.0, output_per_1k=1.0,
                cache_read_per_1k=0.01, cache_creation_per_1k=1.0,
            )
        })
        t = CostTracker(pricing=pricing)
        t.update_from_response(
            type("R", (), {"usage": {"input_tokens": 200, "output_tokens": 0,
                                     "cache_read_input_tokens": 800}})(),
            model="m")
        u = t.state.model_usage["m"]
        assert u.input_tokens == 200
        assert u.cache_read_input_tokens == 800
        assert abs(t.total_cost - 0.208) < 1e-6


class TestCacheNotDoubleBilled:
    """``prompt_tokens`` (OpenAI/DeepSeek) already *includes* the cached
    portion, so it must not also be charged at the cache-read rate. The
    billable full-price quantity is the normalised ``cache_miss_tokens``
    residual."""

    def _pricing(self):
        return PricingTable(custom={
            "m": ModelPricing(
                input_per_1k=1.0, output_per_1k=1.0,
                cache_read_per_1k=0.01, cache_creation_per_1k=1.0,
            )
        })

    def test_deepseek_cached_tokens_billed_once(self):
        from markbot.agent.cache_protocol import CanonicalUsage

        t = CostTracker(pricing=self._pricing())
        # prompt_tokens=1000 total, 800 of them cache hits -> 200 uncached.
        t.add_canonical_usage(CanonicalUsage(
            input_tokens=1000, output_tokens=0,
            cache_read_tokens=800, cache_miss_tokens=200,
        ), model="m")
        # 200 * 1.0/1k + 800 * 0.01/1k == 0.2 + 0.008
        assert abs(t.total_cost - 0.208) < 1e-6

    def test_anthropic_uses_input_tokens_directly(self):
        from markbot.agent.cache_protocol import CanonicalUsage

        t = CostTracker(pricing=self._pricing())
        # Anthropic reports input_tokens as already-uncached and the cache
        # buckets separately, so the residual is absent and input_tokens is
        # the billable figure.
        t.add_canonical_usage(CanonicalUsage(
            input_tokens=200, output_tokens=0,
            cache_read_tokens=800, cache_creation_tokens=0,
            cache_miss_tokens=None,
        ), model="m")
        assert abs(t.total_cost - 0.208) < 1e-6

    def test_openai_nested_cache_read_is_discovered(self):
        from markbot.agent.iteration import normalise_usage

        c = normalise_usage("openai", {
            "prompt_tokens": 2000,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 1500},
        })
        assert c.cache_read_tokens == 1500
        assert c.cache_miss_tokens == 500


class TestSingleAccountingPerResponse:
    """Regression: the main loop used to bill every LLM response twice —
    once via ``_record_canonical_usage`` and again via
    ``_phase_check_budget``'s ``update_from_response`` — so every counter and
    the ``max_budget_usd`` circuit ran at 2x real spend."""

    def test_budget_trips_at_full_spend_not_half(self):
        # claude-sonnet-4-5: $0.003 per 1k input tokens.
        tracker = CostTracker(max_budget_usd=0.005)
        cost = tracker.add_usage(model="claude-sonnet-4-5", input_tokens=1000, output_tokens=0)
        # One call is $0.003 — under the cap. A second identical call
        # ($0.006 total) must trip it. Under the old double-accounting bug a
        # single call already charged $0.006 and tripped here.
        assert abs(cost - 0.003) < 1e-9
        assert not tracker.is_over_budget()
        with pytest.raises(BudgetExceededError):
            tracker.add_usage(model="claude-sonnet-4-5", input_tokens=1000, output_tokens=0)

    def test_api_calls_counter_matches_actual_calls(self):
        tracker = CostTracker()
        class R:
            usage = {"input_tokens": 10, "output_tokens": 5}
        for _ in range(3):
            tracker.update_from_response(R(), model="gpt-4o")
        assert tracker.state.total_api_calls == 3
        u = tracker.state.model_usage["gpt-4o"]
        assert u.input_tokens == 30
        assert u.output_tokens == 15
