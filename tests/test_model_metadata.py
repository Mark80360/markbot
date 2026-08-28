"""Tests for centralized model metadata (markbot/agent/model_metadata.py).

Covers: context-window resolution order (config override > catalog >
global fallback), pricing routing through the existing cost table, and the
effective context window used for compaction thresholds.
"""

from __future__ import annotations

from markbot.agent.cost import PricingTable
from markbot.agent.model_metadata import (
    DEFAULT_CONTEXT_WINDOW,
    get_model_metadata,
    resolve_context_window,
)
from markbot.config.schema import (
    AgentDefaults,
    AgentsConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProvidersConfig,
)


def _config(
    *,
    model_chain: list[str] | None = None,
    context_window_tokens: int = 65_536,
    providers: ProvidersConfig | None = None,
) -> Config:
    return Config(
        providers=providers or ProvidersConfig(),
        agents=AgentsConfig(
            defaults=AgentDefaults(
                model_chain=model_chain or [],
                context_window_tokens=context_window_tokens,
            )
        ),
    )


def _anthropic_provider(*, window: int = 65_536) -> ProvidersConfig:
    return ProvidersConfig(
        anthropic=ProviderConfig(
            api_key="sk-ant",
            models=[ModelConfig(id="opus", name="claude-opus-4-5", context_window=window)],
        )
    )


def test_model_config_override_wins():
    cfg = _config(providers=_anthropic_provider(window=200_000))
    meta = get_model_metadata(
        "anthropic", "claude-opus-4-5", cfg,
        model_cfg=cfg.providers.anthropic.models[0],
    )
    assert meta.source == "config"
    assert meta.context_window == 200_000


def test_catalog_lookup_when_config_unset():
    # context_window left at the schema default → catalog (onboarding DB) wins
    cfg = _config(providers=_anthropic_provider())
    meta = get_model_metadata(
        "anthropic", "claude-opus-4-5", cfg,
        model_cfg=cfg.providers.anthropic.models[0],
    )
    assert meta.source == "catalog"
    assert meta.context_window == 200_000


def test_unknown_model_uses_global_fallback():
    # (provider, model) never seen anywhere → global fallback window
    providers = ProvidersConfig(
        anthropic=ProviderConfig(
            api_key="sk-ant",
            models=[ModelConfig(id="future-model", name="claude-future-9")],
        )
    )
    cfg = _config(context_window_tokens=100_000, providers=providers)
    meta = get_model_metadata(
        "anthropic", "claude-future-9", cfg,
        model_cfg=providers.anthropic.models[0],
    )
    assert meta.source == "default"
    assert meta.context_window == 100_000


def test_metadata_includes_pricing_routed_from_cost_table():
    meta = get_model_metadata("anthropic", "claude-sonnet-4-5")
    expected = PricingTable().get("claude-sonnet-4-5")
    assert meta.source == "catalog"
    assert meta.input_per_1k == expected.input_per_1k
    assert meta.output_per_1k == expected.output_per_1k


def test_resolve_context_window_knows_known_catalog_model():
    # model chain points at a model whose window is only known via catalog
    cfg = _config(
        model_chain=["anthropic/opus"],
        providers=_anthropic_provider(),
    )
    assert resolve_context_window(cfg) == 200_000


def test_resolve_context_window_prefers_explicit_config():
    # explicit non-default per-model window beats the catalog
    cfg = _config(
        model_chain=["anthropic/opus"],
        providers=_anthropic_provider(window=250_000),
    )
    assert resolve_context_window(cfg) == 250_000


def test_resolve_context_window_falls_back_to_global_default():
    cfg = _config(context_window_tokens=88_888)
    assert resolve_context_window(cfg) == 88_888


def test_resolve_context_window_no_config():
    assert resolve_context_window(None) == DEFAULT_CONTEXT_WINDOW