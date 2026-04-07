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


class BudgetExceededError(Exception):
    """Raised when total cost exceeds the configured budget limit."""

    def __init__(self, current_cost: float, budget: float) -> None:
        self.current_cost = current_cost
        self.budget = budget
        super().__init__(
            f"Budget exceeded: ${current_cost:.6f} > ${budget:.6f}"
        )


@dataclass
class ModelUsage:
    """Per-model token usage accumulator."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    api_calls: int = 0
    cost_usd: float = 0.0

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
    "gpt-4o": ModelPricing(0.0025, 0.01, 0.00125, 0.00125),
    "gpt-4o-mini": ModelPricing(0.00015, 0.0006, 0.000075, 0.000075),
    "gpt-4-turbo": ModelPricing(0.01, 0.03, 0.005, 0.005),
    "deepseek-chat": ModelPricing(0.00014, 0.00028, 0.0, 0.0),
    "deepseek-reasoner": ModelPricing(0.00055, 0.00219, 0.0, 0.0),
}


def _match_pricing_key(model_name: str) -> str:
    name = model_name.lower().strip()
    if name in DEFAULT_PRICING:
        return name
    for key in DEFAULT_PRICING:
        if name.startswith(key) or key.startswith(name.split("-")[0]):
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

        self.state.total_cost_usd += cost
        self.state.total_api_calls += 1

        if self.max_budget_usd is not None and self.state.total_cost_usd >= self.max_budget_usd:
            logger.warning(
                "[CostTracker] Budget exceeded: ${:.6f} >= ${:.6f} (model={})",
                self.state.total_cost_usd, self.max_budget_usd, model,
            )
            raise BudgetExceededError(self.state.total_cost_usd, self.max_budget_usd)

        return round(cost, 6)

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

    def reset(self) -> None:
        self.state = CostState()
