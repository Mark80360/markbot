"""Centralized model metadata: context windows + pricing lookup.

A single queryable directory for per-model facts used across the agent:

- **Context windows** — used for compaction thresholds. Previously the
  compaction threshold came from a flat ``agents.defaults.context_window_tokens``
  and per-model ``ModelConfig.context_window`` was effectively dormant.
- **Pricing hints** — routed read-only through ``cost.PricingTable`` so
  ``cost.py`` remains the sole price table (no duplicated pricing data).

Context-window resolution order for a model:
  1. Per-model ``ModelConfig.context_window`` set to a non-default value
     (explicit config override wins).
  2. Built-in catalog (the onboarding model database in
     ``markbot.cli.models.MODEL_DATABASE`` — single source, no copy).
  3. ``agents.defaults.context_window_tokens`` (global fallback).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

DEFAULT_CONTEXT_WINDOW = 65_536
"""Schema-default context window; treated as 'unset' for config overrides."""


@dataclass(frozen=True)
class ModelMetadata:
    """Resolved metadata for one model."""

    provider_id: str
    model_name: str
    context_window: int
    source: str  # "config" | "catalog" | "default"
    input_per_1k: float | None = None
    output_per_1k: float | None = None


@lru_cache(maxsize=1)
def _window_catalog() -> dict[tuple[str, str], int]:
    """(provider_id, model_name|id) -> context window, from the onboarding DB."""
    try:
        from markbot.cli.models import MODEL_DATABASE
    except Exception:
        return {}

    table: dict[tuple[str, str], int] = {}
    for provider_id, provider_models in MODEL_DATABASE.items():
        for m in provider_models.models:
            window = getattr(m, "context_window", 0) or 0
            if window <= 0:
                continue
            table[(provider_id, m.name)] = window
            table[(provider_id, m.id)] = window
    return table


def _pricing_for(model_name: str) -> tuple[float, float] | None:
    """Per-1K-token pricing (input, output) via the existing cost table."""
    try:
        from markbot.agent.cost import PricingTable

        pricing = PricingTable().get(model_name)
        return pricing.input_per_1k, pricing.output_per_1k
    except Exception:
        return None


def get_model_metadata(
    provider_id: str,
    model_name: str,
    config: Any = None,
    model_cfg: Any = None,
) -> ModelMetadata:
    """Resolve metadata for (provider_id, model_name).

    ``model_cfg`` (a resolved ``ModelConfig``) supplies the per-model
    config override; ``config`` supplies the global fallback window.
    """
    # 1. Explicit per-model config override (non-default value).
    if model_cfg is not None:
        window = getattr(model_cfg, "context_window", 0) or 0
        if window != DEFAULT_CONTEXT_WINDOW:
            prices = _pricing_for(model_name)
            return ModelMetadata(provider_id, model_name, window, "config", *prices if prices else (None, None))

    # 2. Built-in catalog.
    catalog = _window_catalog()
    window = catalog.get((provider_id, model_name))
    if window is None and model_cfg is not None:
        window = catalog.get((provider_id, getattr(model_cfg, "id", "")))
    if window:
        prices = _pricing_for(model_name)
        return ModelMetadata(provider_id, model_name, window, "catalog", *prices if prices else (None, None))

    # 3. Global fallback.
    default = DEFAULT_CONTEXT_WINDOW
    if config is not None:
        try:
            default = config.agents.defaults.context_window_tokens
        except Exception:
            pass
    prices = _pricing_for(model_name)
    return ModelMetadata(provider_id, model_name, default, "default", *prices if prices else (None, None))


def resolve_context_window(config: Any, model_ref: str | None = None) -> int:
    """Effective context window for the active model.

    ``model_ref`` defaults to the primary model (``config.primary_model_ref``).
    Resolution: explicit per-model config > catalog > global fallback. Used
    to derive compaction thresholds for the conversation loop.
    """
    if config is None:
        return DEFAULT_CONTEXT_WINDOW
    try:
        fallback = config.agents.defaults.context_window_tokens
    except Exception:
        fallback = DEFAULT_CONTEXT_WINDOW

    ref = model_ref or getattr(config, "primary_model_ref", "") or ""
    if not ref or "/" not in ref:
        return fallback

    try:
        _, model_cfg = config.resolve_model(ref)
    except Exception:
        return fallback

    provider_id = ref.split("/", 1)[0]
    meta = get_model_metadata(provider_id, model_cfg.name or model_cfg.id, config, model_cfg=model_cfg)
    if meta.source in ("config", "catalog"):
        return meta.context_window
    return fallback


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "ModelMetadata",
    "get_model_metadata",
    "resolve_context_window",
]