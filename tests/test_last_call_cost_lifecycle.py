"""Per-call cost accounting must not leak state across iterations.

``_phase_check_budget`` reads ``state.last_call_cost`` and treats ``None`` as
"canonical accounting did not run for this response", falling back to the
legacy reader. Two ways that sentinel went wrong:

* A billing glitch inside ``add_canonical_usage`` returned 0.0 *without*
  touching ``state``, so the previous iteration's figure survived, the
  fallback was skipped, and the budget check degraded to a no-op.
* ``last_call_cost`` was only ever assigned on success, so a stale value also
  produced a misleading "cost this call" log line.

The fixes are: clear the sentinel at the top of ``_phase_call_llm``, and have
every exit path of ``_record_canonical_usage`` set it explicitly.
"""

from __future__ import annotations

import pytest

from markbot.agent.cost import (
    BudgetExceededError,
    CostTracker,
    ModelPricing,
    PricingTable,
)


class _Response:
    def __init__(self, usage: dict) -> None:
        self.usage = usage


class _State:
    """Minimal stand-in for LoopState."""

    def __init__(self, last_call_cost: float | None = None) -> None:
        self.last_call_cost = last_call_cost


def _loop(cost_tracker, model: str = "m") -> object:
    from markbot.agent.iteration import IterationRunner

    loop = IterationRunner.__new__(IterationRunner)
    loop.loop = type("L", (), {"cost_tracker": cost_tracker, "model": model})()
    return loop


def _tracker() -> CostTracker:
    return CostTracker(pricing=PricingTable(custom={
        "m": ModelPricing(input_per_1k=3.0, output_per_1k=15.0),
    }))


class _GlitchyTracker:
    """Raises a non-budget error, mimicking a transient billing failure."""

    def add_canonical_usage(self, canonical, model=None):
        raise RuntimeError("ledger unavailable")


class TestSentinelAlwaysReflectsThisCall:
    def test_success_records_the_spend(self):
        loop = _loop(_tracker())
        state = _State()
        cost = loop._record_canonical_usage(
            _Response({"prompt_tokens": 1000, "completion_tokens": 50}), [], state
        )
        assert cost > 0
        assert state.last_call_cost == cost

    def test_billing_glitch_does_not_leave_a_stale_figure(self):
        """The bug: state kept the previous iteration's cost, so
        ``_phase_check_budget`` saw a non-``None`` sentinel, skipped the
        legacy fallback, and never evaluated the cap."""
        loop = _loop(_GlitchyTracker())
        state = _State(last_call_cost=4.2)
        assert loop._record_canonical_usage(
            _Response({"prompt_tokens": 1000, "completion_tokens": 50}), [], state
        ) == 0.0
        assert state.last_call_cost is None, \
            "stale spend survived a failed call and masked the fallback"

    def test_budget_exceeded_zeroes_rather_than_leaving_stale(self):
        """A cap hit has already been charged, so the budget phase must read a
        real zero and not a leftover figure."""
        tracker = _tracker()
        tracker.max_budget_usd = 0.5
        loop = _loop(tracker)
        state = _State(last_call_cost=99.0)
        with pytest.raises(BudgetExceededError):
            loop._record_canonical_usage(
                _Response({"prompt_tokens": 1000, "completion_tokens": 50}), [], state
            )
        assert state.last_call_cost == 0.0

    @pytest.mark.parametrize("response,usage", [
        (None, None),
        (object(), None),
        (object(), "not-a-dict"),
    ])
    def test_non_billable_response_clears_the_sentinel(self, response, usage):
        """A response with no usage dict is 'not billable', which must read as
        ``None`` (fall back) rather than as a stale number."""
        loop = _loop(_tracker())
        state = _State(last_call_cost=1.5)
        r = None if response is None else type(
            "R", (), {"usage": usage}
        )()
        assert loop._record_canonical_usage(r, [], state) == 0.0
        assert state.last_call_cost is None

    def test_cap_is_enforced_across_repeated_calls(self):
        """End-to-end: the cap must actually stop the spend."""
        tracker = _tracker()
        tracker.max_budget_usd = 1.0
        loop = _loop(tracker)
        state = _State()
        with pytest.raises(BudgetExceededError):
            for _ in range(100):
                loop._record_canonical_usage(
                    _Response({"prompt_tokens": 1000, "completion_tokens": 50}),
                    [], state,
                )
        assert tracker.total_cost > 1.0


class TestModelResolutionDrivesThePrice:
    """``PricingTable`` has no entry for "unknown" and returns a near-zero
    default, so an unresolvable model underbills by ~1000x and stops the cap."""

    def test_configured_model_is_used_when_attempts_are_empty(self):
        t = _tracker()
        loop = _loop(t, model="m")
        loop._record_canonical_usage(
            _Response({"prompt_tokens": 1000, "completion_tokens": 50}), [], _State()
        )
        assert t.total_cost == pytest.approx(3.75)

    def test_attempt_model_overrides_the_configured_default(self):
        class _M:
            name = "m"

        class _A:
            model_ref = None
            model = _M()

        t = _tracker()
        loop = _loop(t, model="some-other-model")
        loop._record_canonical_usage(
            _Response({"prompt_tokens": 1000, "completion_tokens": 50}), [_A()], _State()
        )
        assert "m" in t.state.model_usage
        assert t.total_cost == pytest.approx(3.75)

    def test_model_ref_is_preferred_when_present(self):
        class _A:
            model_ref = "m"
            model = None

        t = _tracker()
        loop = _loop(t, model="other")
        loop._record_canonical_usage(
            _Response({"prompt_tokens": 1000, "completion_tokens": 50}), [_A()], _State()
        )
        assert t.total_cost == pytest.approx(3.75)

    def test_unknown_model_is_not_silently_underbilled(self):
        """With nothing to resolve from, the near-zero default is still used,
        but the loop must not pretend the spend was real."""
        t = _tracker()
        loop = _loop(t, model="")
        loop._record_canonical_usage(
            _Response({"prompt_tokens": 1000, "completion_tokens": 50}), [], _State()
        )
        # Recorded, but under 'unknown' — cheap, yet visible in the breakdown
        # rather than silently folded into a priced model.
        assert "unknown" in t.state.model_usage
