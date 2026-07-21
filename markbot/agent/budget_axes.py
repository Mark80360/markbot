"""Three-axis runtime budget: tokens / dollars / wall-time.

Complements CostTracker (USD) with iteration-local wall-time and optional
token ceilings so long turns can halt cleanly with a residual summary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class BudgetAxis(str, Enum):
    NONE = "none"
    USD = "usd"
    TOKENS = "tokens"
    WALL_TIME = "wall_time"
    ITERATIONS = "iterations"


@dataclass(frozen=True)
class BudgetAxisHit:
    axis: BudgetAxis = BudgetAxis.NONE
    message: str = ""
    current: float = 0.0
    limit: float = 0.0

    @property
    def hit(self) -> bool:
        return self.axis is not BudgetAxis.NONE


@dataclass
class RuntimeBudgetConfig:
    """Hard limits evaluated each iteration (in addition to CostTracker)."""

    max_wall_seconds: float | None = None
    max_total_tokens: int | None = None

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "RuntimeBudgetConfig":
        if settings is None:
            return cls()
        if isinstance(settings, Mapping):
            get = settings.get
        else:
            def get(k, d=None):
                return getattr(settings, k, d)

        wall = get("max_wall_seconds", get("maxWallSeconds", None))
        tokens = get("max_total_tokens", get("maxTotalTokens", None))
        return cls(
            max_wall_seconds=float(wall) if wall not in (None, "") else None,
            max_total_tokens=int(tokens) if tokens not in (None, "") else None,
        )


@dataclass
class RuntimeBudget:
    """Per-turn wall-time / token ceiling tracker."""

    config: RuntimeBudgetConfig = field(default_factory=RuntimeBudgetConfig)
    started_at: float = field(default_factory=time.monotonic)
    total_tokens: int = 0

    def reset(self) -> None:
        self.started_at = time.monotonic()
        self.total_tokens = 0

    def add_tokens(self, n: int) -> None:
        if n and n > 0:
            self.total_tokens += int(n)

    def evaluate(self) -> BudgetAxisHit:
        cfg = self.config
        if cfg.max_wall_seconds is not None:
            elapsed = time.monotonic() - self.started_at
            if elapsed >= cfg.max_wall_seconds:
                return BudgetAxisHit(
                    axis=BudgetAxis.WALL_TIME,
                    message=(
                        f"Wall-time budget exceeded: {elapsed:.1f}s "
                        f">= {cfg.max_wall_seconds:.1f}s"
                    ),
                    current=elapsed,
                    limit=cfg.max_wall_seconds,
                )
        if cfg.max_total_tokens is not None and self.total_tokens >= cfg.max_total_tokens:
            return BudgetAxisHit(
                axis=BudgetAxis.TOKENS,
                message=(
                    f"Token budget exceeded: {self.total_tokens} "
                    f">= {cfg.max_total_tokens}"
                ),
                current=float(self.total_tokens),
                limit=float(cfg.max_total_tokens),
            )
        return BudgetAxisHit()
