"""Tests for the unified auxiliary model client (markbot/agent/auxiliary.py).

Covers: no-configuration fallback, per-task overrides, auto chain resolution
(default_aux_model of configured providers, then main model), credit/payment
exhaustion auto-degradation to the next provider, and failure semantics.
"""

from __future__ import annotations

import pytest

from markbot.agent.auxiliary import (
    AuxiliaryCallError,
    get_async_text_auxiliary_client,
    resolve_task_chain,
)
from markbot.config.schema import (
    AgentDefaults,
    AgentsConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProvidersConfig,
)
from markbot.providers.base import LLMResponse


def _config(
    model_chain: list[str] | None = None,
    auxiliary: dict | None = None,
    providers: ProvidersConfig | None = None,
) -> Config:
    return Config(
        agents=AgentsConfig(
            defaults=AgentDefaults(
                model_chain=model_chain or [],
                auxiliary=auxiliary or {},
            )
        ),
        providers=providers or ProvidersConfig(),
    )


def _two_aux_providers() -> ProvidersConfig:
    """deepseek + zhipu both configured → both declare a default_aux_model."""
    return ProvidersConfig(
        deepseek=ProviderConfig(api_key="sk-ds"),
        zhipu=ProviderConfig(api_key="sk-zp"),
    )


def _patch_config(monkeypatch, cfg: Config):
    import markbot.agent.auxiliary as aux_mod

    monkeypatch.setattr(aux_mod, "_get_config", lambda: cfg)


class _FakeRetry:
    """Stand-in for LLMProvider.chat_with_retry returning canned responses."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def chat_with_retry(
        self, messages=None, tools=None, model=None, max_tokens=None, temperature=None, **kwargs
    ) -> LLMResponse:
        self.calls.append(model)
        return self._responses.pop(0)


def test_no_configuration_returns_none(monkeypatch):
    _patch_config(monkeypatch, _config())

    client, model = get_async_text_auxiliary_client("web_extract")
    assert client is None
    assert model is None
    assert resolve_task_chain("web_extract") == []


def test_unconfigured_providers_ignored_in_auto_chain(monkeypatch):
    # providers exist but have no API key → nothing usable
    _patch_config(monkeypatch, _config(providers=ProvidersConfig(deepseek=ProviderConfig())))

    client, model = get_async_text_auxiliary_client("web_extract")
    assert client is None
    assert model is None


def test_auto_chain_accepts_secondary_credentials_only(monkeypatch):
    # a provider with only ``api_keys`` (no primary api_key) is still usable
    _patch_config(
        monkeypatch,
        _config(providers=ProvidersConfig(deepseek=ProviderConfig(api_keys=["sk-extra"]))),
    )

    assert resolve_task_chain("web_extract") == [("deepseek", "deepseek-chat")]


def test_per_task_override_pins_the_task(monkeypatch):
    cfg = _config(
        auxiliary={"web_extract": {"provider": "deepseek", "model": "deepseek-chat"}},
        providers=_two_aux_providers(),
    )
    _patch_config(monkeypatch, cfg)

    client, model = get_async_text_auxiliary_client("web_extract")
    assert client is not None
    assert model == "deepseek-chat"
    assert resolve_task_chain("web_extract") == [("deepseek", "deepseek-chat")]

    # an unpinned task still resolves through the auto chain
    assert resolve_task_chain("other_task") == [
        ("deepseek", "deepseek-chat"),
        ("zhipu", "glm-4-flash"),
    ]


def test_auto_chain_uses_configured_default_aux_models(monkeypatch):
    _patch_config(monkeypatch, _config(providers=_two_aux_providers()))

    assert resolve_task_chain("web_extract") == [
        ("deepseek", "deepseek-chat"),
        ("zhipu", "glm-4-flash"),
    ]


def test_auto_chain_falls_back_to_main_model(monkeypatch):
    cfg = _config(
        model_chain=["openai/gpt-4o"],
        providers=ProvidersConfig(
            deepseek=ProviderConfig(api_key="sk-ds"),
            openai=ProviderConfig(
                api_key="sk-oa",
                models=[ModelConfig(id="gpt-4o", name="gpt-4o")],
            ),
        ),
    )
    _patch_config(monkeypatch, cfg)

    chain = resolve_task_chain("web_extract")
    assert ("deepseek", "deepseek-chat") in chain
    assert chain[-1] == ("openai", "gpt-4o")


async def test_credit_exhaustion_degrades_to_next_provider(monkeypatch):
    _patch_config(monkeypatch, _config(providers=_two_aux_providers()))

    fake = _FakeRetry(
        [
            LLMResponse(
                content=(
                    "You exceeded your current quota. "
                    "Please check your plan and billing details."
                ),
                finish_reason="error",
            ),
            LLMResponse(content="concise summary", finish_reason="stop"),
        ]
    )
    import markbot.agent.auxiliary as aux_mod

    monkeypatch.setattr(aux_mod, "_make_provider", lambda ref: fake)

    client, default_model = get_async_text_auxiliary_client("web_extract")
    response = await client.chat.completions.create(
        model=default_model,
        messages=[{"role": "user", "content": "summarize this"}],
    )

    assert fake.calls == ["deepseek-chat", "glm-4-flash"]
    assert response.choices[0].message.content == "concise summary"


async def test_provider_exception_degrades_to_next_provider(monkeypatch):
    _patch_config(monkeypatch, _config(providers=_two_aux_providers()))

    class _Raising:
        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            raise RuntimeError("connection reset")

    class _Ok:
        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            return LLMResponse(content="fine", finish_reason="stop")

    import markbot.agent.auxiliary as aux_mod

    monkeypatch.setattr(
        aux_mod,
        "_make_provider",
        lambda ref: _Raising() if ref.provider_id == "deepseek" else _Ok(),
    )

    client, _ = get_async_text_auxiliary_client("web_extract")
    response = await client.chat.completions.create(
        messages=[{"role": "user", "content": "hi"}]
    )
    assert response.choices[0].message.content == "fine"


async def test_error_tries_all_providers_then_raises(monkeypatch):
    _patch_config(monkeypatch, _config(providers=_two_aux_providers()))

    fake = _FakeRetry(
        [
            LLMResponse(content="invalid request body", finish_reason="error"),
            LLMResponse(content="invalid request body", finish_reason="error"),
        ]
    )
    import markbot.agent.auxiliary as aux_mod

    monkeypatch.setattr(aux_mod, "_make_provider", lambda ref: fake)

    client, _ = get_async_text_auxiliary_client("web_extract")
    with pytest.raises(AuxiliaryCallError):
        await client.chat.completions.create(messages=[{"role": "user", "content": "hi"}])
    # every provider was attempted once; failure only surfaces at the end
    assert fake.calls == ["deepseek-chat", "glm-4-flash"]


async def test_empty_response_raises(monkeypatch):
    _patch_config(monkeypatch, _config(providers=_two_aux_providers()))

    fake = _FakeRetry([LLMResponse(content=None, finish_reason="stop")])
    import markbot.agent.auxiliary as aux_mod

    monkeypatch.setattr(aux_mod, "_make_provider", lambda ref: fake)

    client, _ = get_async_text_auxiliary_client("web_extract")
    with pytest.raises(AuxiliaryCallError):
        await client.chat.completions.create(messages=[{"role": "user", "content": "hi"}])