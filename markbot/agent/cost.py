"""Cost tracking and budget control for LLM API usage.

Tracks token consumption across models, converts to USD using
per-model pricing tables, and enforces optional budget caps.

Usage:
    tracker = CostTracker(max_budget_usd=1.00)
    tracker.add_usage(model="claude-sonnet-4-5", input_tokens=1000, output_tokens=500)
    if tracker.is_over_budget():
        raise BudgetExceededError(tracker.get_total_cost())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from markbot.types.exceptions import BudgetExceededError
from markbot.agent.cache_protocol import CanonicalUsage


@dataclass
class ModelUsage:
    """Per-model token usage accumulator."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    api_calls: int = 0
    cost_usd: float = 0.0
    # Snapshot of cache_read_input_tokens from the most recent add_usage
    # call.  Used by last_turn_cache_savings to report per-turn savings
    # rather than the cumulative total.
    last_call_cache_read_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass
class CostState:
    """Aggregated cost state."""

    total_cost_usd: float = 0.0
    total_api_calls: int = 0
    model_usage: dict[str, ModelUsage] = field(default_factory=dict)


@dataclass
class ModelPricing:
    """Per-1K-token pricing for a single model (USD)."""

    input_per_1k: float = 0.003
    output_per_1k: float = 0.006
    cache_read_per_1k: float = 0.0003
    cache_creation_per_1k: float = 0.0006


DEFAULT_PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-5": ModelPricing(0.015, 0.075, 0.0015, 0.018),
    "claude-opus-4": ModelPricing(0.015, 0.075, 0.0015, 0.018),
    "claude-sonnet-4-5": ModelPricing(0.003, 0.015, 0.0003, 0.00375),
    "claude-sonnet-4": ModelPricing(0.003, 0.015, 0.0003, 0.00375),
    "claude-haiku-4-5": ModelPricing(0.001, 0.005, 0.0001, 0.00125),
    "claude-haiku-4": ModelPricing(0.001, 0.005, 0.0001, 0.00125),
    "claude-3-7-sonnet": ModelPricing(0.003, 0.015, 0.0003, 0.00375),
    "claude-3-5-sonnet": ModelPricing(0.003, 0.015, 0.0003, 0.00375),
    "claude-3-5-haiku": ModelPricing(0.00025, 0.00125, 0.000025, 0.0003125),
    "claude-3-opus": ModelPricing(0.015, 0.075, 0.0015, 0.018),
    "gpt-4o": ModelPricing(0.0025, 0.01, 0.00125, 0.0025),
    "gpt-4o-mini": ModelPricing(0.00015, 0.0006, 0.000075, 0.00015),
    "gpt-4-turbo": ModelPricing(0.01, 0.03, 0.005, 0.01),
    "deepseek-chat": ModelPricing(0.00014, 0.00028, 0.0, 0.0),
    "deepseek-reasoner": ModelPricing(0.00055, 0.00219, 0.0, 0.0),
}


def _match_pricing_key(model_name: str) -> str:
    name = model_name.lower().strip()
    if name in DEFAULT_PRICING:
        return name

    for key in sorted(DEFAULT_PRICING, key=len, reverse=True):
        if name.startswith(key):
            return key

    if "opus" in name:
        return "claude-opus-4-5"
    if "sonnet" in name:
        return "claude-sonnet-4-5"
    if "haiku" in name:
        return "claude-haiku-4-5"
    if "gpt-4o-mini" in name or "gpt-4.1-mini" in name:
        return "gpt-4o-mini"
    if "gpt-4o" in name or "gpt-4.1" in name:
        return "gpt-4o"
    if "deepseek" in name:
        return "deepseek-chat"
    return "default"


_DEFAULT_PRICING_OBJ = ModelPricing()


class PricingTable:
    """Lookup pricing by model name with custom overrides."""

    def __init__(self, custom: dict[str, ModelPricing] | None = None):
        self._custom: dict[str, ModelPricing] = custom or {}

    def get(self, model: str) -> ModelPricing:
        if model in self._custom:
            return self._custom[model]
        key = _match_pricing_key(model)
        return DEFAULT_PRICING.get(key, _DEFAULT_PRICING_OBJ)

    def set_custom(self, model: str, pricing: ModelPricing) -> None:
        self._custom[model] = pricing


class CostTracker:
    """Tracks LLM usage and costs with optional budget enforcement.

    Args:
        max_budget_usd: If set, :meth:`add_usage` will raise
            :class:`BudgetExceededError` when exceeded.
        pricing: Custom pricing table; defaults to built-in table.
    """

    def __init__(
        self,
        max_budget_usd: float | None = None,
        warn_threshold_usd: float = 0.5,
        pricing: PricingTable | None = None,
    ) -> None:
        self.max_budget_usd = max_budget_usd
        self.warn_threshold_usd = warn_threshold_usd
        self.pricing = pricing or PricingTable()
        self.state = CostState()
        self._warn_emitted: bool = False

    @property
    def total_cost(self) -> float:
        return round(self.state.total_cost_usd, 6)

    def is_over_budget(self) -> bool:
        if self.max_budget_usd is None:
            return False
        return self.state.total_cost_usd >= self.max_budget_usd

    def add_usage(
        self,
        *,
        model: str = "unknown",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        api_duration_ms: int = 0,
    ) -> float:
        """Record a single API response's usage and return its cost.

        Raises:
            BudgetExceededError: If ``max_budget_usd`` is set and this call
                pushes the total over the limit.
        """
        price = self.pricing.get(model)
        cost = (
            (input_tokens / 1000.0) * price.input_per_1k
            + (output_tokens / 1000.0) * price.output_per_1k
            + (cache_read_input_tokens / 1000.0) * price.cache_read_per_1k
            + (cache_creation_input_tokens / 1000.0) * price.cache_creation_per_1k
        )

        mu = self.state.model_usage.setdefault(model, ModelUsage())
        mu.input_tokens += input_tokens
        mu.output_tokens += output_tokens
        mu.cache_creation_input_tokens += cache_creation_input_tokens
        mu.cache_read_input_tokens += cache_read_input_tokens
        mu.api_calls += 1
        mu.cost_usd += cost
        # Per-call snapshot so last_turn_cache_savings can compute the
        # most recent turn's savings rather than the cumulative total.
        mu.last_call_cache_read_tokens = cache_read_input_tokens

        self.state.total_cost_usd += cost
        self.state.total_api_calls += 1

        if (
            self.warn_threshold_usd > 0
            and not self._warn_emitted
            and self.state.total_cost_usd >= self.warn_threshold_usd
        ):
            self._warn_emitted = True
            logger.warning(
                "[CostTracker] Cost warning: ${:.6f} (threshold=${:.2f})",
                self.state.total_cost_usd,
                self.warn_threshold_usd,
            )

        if self.max_budget_usd is not None and self.state.total_cost_usd >= self.max_budget_usd:
            logger.warning(
                "[CostTracker] Budget exceeded: ${:.6f} >= ${:.6f} (model={})",
                self.state.total_cost_usd, self.max_budget_usd, model,
            )
            raise BudgetExceededError(self.state.total_cost_usd, self.max_budget_usd)

        return round(cost, 6)

    def add_canonical_usage(
        self,
        usage: CanonicalUsage,
        *,
        model: str = "unknown",
    ) -> float:
        """Record a :class:`CanonicalUsage` snapshot.

        Falls back to the per-field ``add_usage`` path for any
        provider that still hands us the legacy dict shape.  This is
        the preferred entry point once a :class:`CanonicalUsage` is
        available — it transparently handles the OpenAI / DeepSeek /
        Anthropic bucket-name differences.
        """
        return self.add_usage(
            model=model,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_creation_input_tokens=usage.cache_creation_tokens or 0,
            cache_read_input_tokens=usage.cache_read_tokens or 0,
        )

    def update_from_response(self, response: Any, model: str = "unknown") -> float:
        """Convenience: extract usage from a provider response object.

        Supports Anthropic SDK responses and dicts with a ``usage`` attr/key.
        Returns the cost of this call.
        """
        usage_data = getattr(response, "usage", None)
        if not usage_data:
            try:
                usage_data = response.get("usage") if isinstance(response, dict) else None
            except (AttributeError, TypeError):
                pass

        if not usage_data:
            return 0.0

        def _extract(key: str, default: int = 0) -> int:
            if isinstance(usage_data, dict):
                return int(usage_data.get(key, default) or default)
            return int(getattr(usage_data, key, default) or default)

        return self.add_usage(
            model=model,
            input_tokens=_extract("input_tokens"),
            output_tokens=_extract("output_tokens"),
            cache_creation_input_tokens=_extract("cache_creation_input_tokens"),
            cache_read_input_tokens=_extract("cache_read_input_tokens"),
        )

    def get_model_usage(self, model: str) -> ModelUsage | None:
        return self.state.model_usage.get(model)

    def last_turn_cache_savings(self, model: str | None = None) -> Optional[float]:
        """Estimate USD saved by the *most recent* call's cache reads.

        Uses ``ModelUsage.last_call_cache_read_tokens`` — a per-call
        snapshot updated on every :meth:`add_usage` — so the returned
        value reflects only the most recent call, not the cumulative
        session total.

        When ``model`` is ``None`` we pick the most recently *added*
        model in :attr:`CostState.model_usage` (insertion order is
        preserved in Python 3.7+ dicts).  Returns ``None`` if there
        is no data or the most recent call had no cache reads.
        """
        if not self.state.model_usage:
            return None
        target_model = model
        if target_model is None:
            # Pick the most recently added model.  Dicts preserve
            # insertion order; reversed() walks from newest to oldest.
            for name in reversed(list(self.state.model_usage.keys())):
                target_model = name
                break
        if target_model is None:
            return None
        mu = self.state.model_usage.get(target_model)
        if mu is None or mu.last_call_cache_read_tokens == 0:
            return None
        price = self.pricing.get(target_model)
        # Per-call savings = (input price - cache_read price) * cached tokens.
        saved = (mu.last_call_cache_read_tokens / 1000.0) * (
            price.input_per_1k - price.cache_read_per_1k
        )
        return round(max(0.0, saved), 6)

    def get_summary(self) -> dict[str, Any]:
        models = {}
        for name, mu in self.state.model_usage.items():
            models[name] = {
                "input_tokens": mu.input_tokens,
                "output_tokens": mu.output_tokens,
                "cache_creation_input_tokens": mu.cache_creation_input_tokens,
                "cache_read_input_tokens": mu.cache_read_input_tokens,
                "total_tokens": mu.total_tokens,
                "api_calls": mu.api_calls,
                "cost_usd": round(mu.cost_usd, 6),
            }
        return {
            "total_cost_usd": round(self.state.total_cost_usd, 6),
            "budget_limit_usd": self.max_budget_usd,
            "over_budget": self.is_over_budget(),
            "total_api_calls": self.state.total_api_calls,
            "models": models,
        }

    def get_token_summary(self) -> dict[str, Any]:
        """Get token usage summary compatible with former TokenTracker.get_summary()."""
        total = ModelUsage()
        for mu in self.state.model_usage.values():
            total.input_tokens += mu.input_tokens
            total.output_tokens += mu.output_tokens
            total.cache_creation_input_tokens += mu.cache_creation_input_tokens
            total.cache_read_input_tokens += mu.cache_read_input_tokens
            total.api_calls += mu.api_calls
        return {
            "total": {
                "input_tokens": total.input_tokens,
                "output_tokens": total.output_tokens,
                "cache_creation_input_tokens": total.cache_creation_input_tokens,
                "cache_read_input_tokens": total.cache_read_input_tokens,
                "total_tokens": total.total_tokens,
            },
            "api_calls": total.api_calls,
            "average_per_call": {
                "input_tokens": total.input_tokens // total.api_calls if total.api_calls else 0,
                "output_tokens": total.output_tokens // total.api_calls if total.api_calls else 0,
            },
        }

    def reset(self) -> None:
        self.state = CostState()
