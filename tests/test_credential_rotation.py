"""Tests for multi-key credential rotation in the fallback chain.

Covers: ``ProviderConfig.credential_keys`` normalization, rotation on
credit exhaustion / per-key rate limits (same model retried with the next
key before moving on), circuit protection when a spare key saves the model,
and non-rotatable errors failing fast.
"""

from __future__ import annotations

import pytest

from markbot.config.schema import (
    AgentDefaults,
    AgentsConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProvidersConfig,
)
from markbot.providers.base import LLMResponse
from markbot.providers.fallback import AllModelsFailedError, FallbackManager


def _manager(*, chain: list[str], provider_config: ProviderConfig) -> FallbackManager:
    config = Config(
        providers=ProvidersConfig(deepseek=provider_config),
        agents=AgentsConfig(defaults=AgentDefaults(model_chain=list(chain))),
    )
    return FallbackManager(config)


class TestCredentialKeys:
    def test_primary_then_extras_in_order(self):
        pc = ProviderConfig(api_key="k1", api_keys=["k2", "k3"])
        assert pc.credential_keys == ["k1", "k2", "k3"]

    def test_dedupes_and_drops_empty(self):
        pc = ProviderConfig(api_key="k1", api_keys=["", "k1", "k2"])
        assert pc.credential_keys == ["k1", "k2"]

    def test_extras_only(self):
        pc = ProviderConfig(api_keys=["k2", "k3"])
        assert pc.credential_keys == ["k2", "k3"]


class TestRotation:
    @staticmethod
    def _provider_config() -> ProviderConfig:
        return ProviderConfig(
            api_key="primary-key",
            api_keys=["spare-key"],
            models=[ModelConfig(id="r1", name="deepseek-reasoner")],
        )

    @staticmethod
    async def _run(mgr: FallbackManager, tried: list, mock_caller):
        return await mgr._try_chain(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            tool_choice=None,
            max_tokens=None,
            temperature=None,
            caller=mock_caller,
        )

    async def test_credit_exhaustion_rotates_to_next_credential(self):
        mgr = _manager(
            chain=["deepseek/r1"],
            provider_config=self._provider_config(),
        )
        tried = []

        def fake_get(pc, pn, api_key=None, credential_variant=0):
            tried.append((credential_variant, api_key))
            return object()

        mgr._get_or_create_provider = fake_get

        async def mock_caller(provider, model_config, mt, temp, reasoning, msgs):
            if len(tried) == 1:
                return LLMResponse(
                    content=(
                        "You exceeded your current quota. "
                        "Please check your plan and billing details."
                    ),
                    finish_reason="error",
                )
            return LLMResponse(content="ok", finish_reason="stop")

        response, attempts = await self._run(mgr, tried, mock_caller)

        assert [v for v, _ in tried] == [0, 1]
        assert tried[1][1] == "spare-key"
        assert response.content == "ok"
        assert attempts[-1].success is True
        # a dead primary key must NOT trip the circuit on a healthy model
        assert mgr._get_circuit("deepseek/r1").failure_count == 0

    async def test_rate_limit_rotates_to_next_credential(self):
        mgr = _manager(
            chain=["deepseek/r1"],
            provider_config=self._provider_config(),
        )
        tried = []

        def fake_get(pc, pn, api_key=None, credential_variant=0):
            tried.append(credential_variant)
            return object()

        mgr._get_or_create_provider = fake_get

        async def mock_caller(provider, model_config, mt, temp, reasoning, msgs):
            if len(tried) == 1:
                return LLMResponse(
                    content="Rate limit exceeded, please try again later",
                    finish_reason="error",
                )
            return LLMResponse(content="ok", finish_reason="stop")

        response, _ = await self._run(mgr, tried, mock_caller)
        assert tried == [0, 1]
        assert response.content == "ok"

    async def test_all_credentials_fail_then_next_model(self):
        config = Config(
            providers=ProvidersConfig(
                deepseek=self._provider_config(),
                openai=ProviderConfig(
                    api_key="ok-key",
                    models=[ModelConfig(id="g4", name="gpt-4o")],
                ),
            ),
            agents=AgentsConfig(
                defaults=AgentDefaults(model_chain=["deepseek/r1", "openai/g4"])
            ),
        )
        mgr = FallbackManager(config)
        tried = []

        def fake_get(pc, pn, api_key=None, credential_variant=0):
            tried.append((pn, credential_variant))
            return object()

        mgr._get_or_create_provider = fake_get

        async def mock_caller(provider, model_config, mt, temp, reasoning, msgs):
            if model_config.name == "deepseek-reasoner":
                return LLMResponse(
                    content="Insufficient credits for this request",
                    finish_reason="error",
                )
            return LLMResponse(content="ok from openai", finish_reason="stop")

        response, attempts = await self._run(mgr, tried, mock_caller)

        # both deepseek credentials were exhausted, then the chain moved on
        assert tried == [("deepseek", 0), ("deepseek", 1), ("openai", 0)]
        assert response.content == "ok from openai"
        assert attempts[0].success is False
        assert attempts[1].success is True
        # every credential dead = the model itself is unreachable → circuit may trip
        assert mgr._get_circuit("deepseek/r1").failure_count == 1

    async def test_format_error_does_not_rotate(self):
        mgr = _manager(
            chain=["deepseek/r1"],
            provider_config=self._provider_config(),
        )
        tried = []

        def fake_get(pc, pn, api_key=None, credential_variant=0):
            tried.append(credential_variant)
            return object()

        mgr._get_or_create_provider = fake_get

        async def mock_caller(provider, model_config, mt, temp, reasoning, msgs):
            return LLMResponse(content="invalid request body", finish_reason="error")

        with pytest.raises(AllModelsFailedError):
            await self._run(mgr, tried, mock_caller)

        # format errors are not credential-specific → single attempt
        assert tried == [0]