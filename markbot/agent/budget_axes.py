"""Three-axis runtime budget: tokens / dollars / inactivity.

Replaces wall-time budget with inactivity-based timeout: the turn is killed
only when the agent stops producing activity (LLM tokens, tool calls,
progress callbacks) for the configured duration, not when the total turn
wall-time exceeds a ceiling.  A separate watchdog thread polls the
inactivity clock and requests interruption when the timeout fires.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Default per-turn ceilings
# ---------------------------------------------------------------------------
# 1800 s (30 min) of *inactivity* matches Hermes' gateway_timeout default.
# A busy turn that produces tokens / tool calls / progress continuously
# can run indefinitely; only a truly hung API call or deadlocked tool gets
# killed.  Set to 0 (or a negative number) to disable.
DEFAULT_MAX_INACTIVITY_SECONDS = 1800.0
DEFAULT_MAX_TOTAL_TOKENS = 3_000_000


class BudgetAxis(str, Enum):
    NONE = "none"
    USD = "usd"
    TOKENS = "tokens"
    INACTIVITY = "inactivity"
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
    """Hard limits evaluated each iteration.

    ``max_inactivity_seconds``: turn is killed after this many seconds of
    zero activity (LLM tokens, tool calls, progress callbacks).  A busy
    turn that keeps producing activity runs indefinitely.

    ``max_total_tokens``: hard ceiling on cumulative prompt+completion
    tokens across all LLM calls in a single turn.
    """

    max_inactivity_seconds: float | None = DEFAULT_MAX_INACTIVITY_SECONDS
    max_total_tokens: int | None = DEFAULT_MAX_TOTAL_TOKENS

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "RuntimeBudgetConfig":
        if settings is None:
            return cls()
        if isinstance(settings, Mapping):
            get = settings.get
        else:

            def get(k, d=None):
                return getattr(settings, k, d)

        inactivity = get(
            "max_inactivity_seconds", get("maxInactivitySeconds", None)
        )
        tokens = get("max_total_tokens", get("maxTotalTokens", None))
        return cls(
            max_inactivity_seconds=_ceiling_or_disabled(
                inactivity, DEFAULT_MAX_INACTIVITY_SECONDS
            ),
            max_total_tokens=_ceiling_or_disabled(
                tokens, DEFAULT_MAX_TOTAL_TOKENS
            ),
        )


def _ceiling_or_disabled(value: Any, default: float) -> Any:
    """Normalise a configured ceiling.

    - ``None`` / ``""`` (unset) → the built-in default (axis stays ON).
    - ``0`` or negative → ``None`` (axis explicitly disabled).
    - anything else → the numeric value.
    """
    if value in (None, ""):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num <= 0:
        return None
    return int(num) if isinstance(default, int) else num


@dataclass
class RuntimeBudget:
    """Per-turn inactivity / token ceiling tracker.

    ``touch_activity()`` is called on every LLM token, tool call, and
    progress callback.  ``evaluate()`` compares ``seconds_since_activity``
    against the configured threshold, *not* the total wall time since
    the turn started.

    ``interrupt_requested`` is set by an external watchdog thread that
    polls ``seconds_since_activity`` independently of the asyncio event
    loop, so it fires even when a blocking tool call starves the loop.
    """

    config: RuntimeBudgetConfig = field(default_factory=RuntimeBudgetConfig)
    _last_activity_at: float = field(default_factory=time.monotonic)
    total_tokens: int = 0
    # Thread-safe flag for watchdog → agent loop communication.
    # Set to True when the watchdog detects inactivity timeout.
    interrupt_requested: bool = False

    @property
    def seconds_since_activity(self) -> float:
        """Wall-clock seconds since the last ``touch_activity()`` call."""
        return time.monotonic() - self._last_activity_at

    def touch_activity(self) -> None:
        """Refresh the activity clock.

        Thread-safe: ``_last_activity_at`` is a C-level float write on
        CPython (single store instruction on 64-bit), so worst case is a
        stale read of a few ns — harmless for a 1800 s threshold.
        """
        self._last_activity_at = time.monotonic()

    def reset(self) -> None:
        """Start a fresh turn: reset activity clock, tokens, and flags."""
        self._last_activity_at = time.monotonic()
        self.total_tokens = 0
        self.interrupt_requested = False

    def add_tokens(self, n: int) -> None:
        if n and n > 0:
            self.total_tokens += int(n)

    def evaluate(self) -> BudgetAxisHit:
        cfg = self.config
        if cfg.max_inactivity_seconds is not None:
            idle = self.seconds_since_activity
            if idle >= cfg.max_inactivity_seconds:
                return BudgetAxisHit(
                    axis=BudgetAxis.INACTIVITY,
                    message=(
                        f"Inactivity timeout: {idle:.1f}s idle "
                        f">= {cfg.max_inactivity_seconds:.1f}s"
                    ),
                    current=idle,
                    limit=cfg.max_inactivity_seconds,
                )
        if (
            cfg.max_total_tokens is not None
            and self.total_tokens >= cfg.max_total_tokens
        ):
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
