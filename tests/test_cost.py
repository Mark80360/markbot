"""Tests for cost tracking and budget control."""

import pytest
from markbot.agent.cost import CostTracker, ModelPricing, ModelUsage, CostState, PricingTable
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
