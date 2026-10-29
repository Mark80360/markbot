"""Regression tests for the 429 / rate-limit recovery path.

The failure these lock in (observed with a relay that enforces TPM/RPM):

    Provider error: RateLimitError: Error code: 429 -
    {'error': {'message': 'inference exceeds tpm/rpm limit',
               'type': 'rate_limit_error', 'code': 'insufficient_quota'}}

Two defects made that error both noisy and unrecoverable:

1. ``LLMProvider.chat_with_retry`` retried the *same* model with a 5s/10s
   backoff. A TPM/RPM budget is a rolling minute, so the retries could not
   succeed — and each one re-sent the full ~31k-token prompt against the
   budget that was already exhausted. The README promises the opposite:
   限流时自动切换下一个模型.
2. When every model failed, ``AllModelsFailedError`` rendered only
   "All N models in chain failed", hiding the upstream 429 body from the
   user. Nothing was recoverable and nothing explained why.
"""

from __future__ import annotations

import asyncio

import pytest

from markbot.config.schema import (
    AgentDefaults,
    AgentsConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProvidersConfig,
)
from markbot.providers.base import LLMProvider, LLMResponse
from markbot.providers.error_classifier import (
    RATE_LIMIT_WINDOW_SECONDS,
    ErrorCategory,
    classify_api_error,
)
from markbot.providers.errors import ErrorType
from markbot.providers.fallback import AllModelsFailedError, FallbackManager

# The exact body the upstream relay returned, including the misleading
# ``insufficient_quota`` code that accompanies a per-minute TPM limit.
TPM_429_BODY = (
    "Error code: 429 - {'error': {'message': 'inference exceeds tpm/rpm limit', "
    "'type': 'rate_limit_error', 'param': '', 'code': 'insufficient_quota'}}"
)
QUOTA_429_BODY = (
    "Error code: 429 - {'error': {'message': 'You exceeded your current quota, "
    "please check your plan and billing details', 'code': 'insufficient_quota'}}"
)


class TestRateLimitClassification:
    def test_tpm_429_is_transient_rate_limit(self):
        result = classify_api_error(429, TPM_429_BODY)
        assert result.reason == "rate_limit"
        assert result.category is ErrorCategory.TRANSIENT
        assert result.retryable
        # Rate limits are per-credential: the chain must be allowed to rotate
        # instead of retrying the same exhausted key.
        assert result.should_fallback

    def test_tpm_429_backoff_covers_the_window(self):
        result = classify_api_error(429, TPM_429_BODY)
        assert result.backoff_seconds == RATE_LIMIT_WINDOW_SECONDS
        assert result.backoff_seconds >= 60.0

    def test_quota_only_429_is_deterministic_billing(self):
        """A 429 that only reports exhausted quota is not a window.

        ``insufficient_quota`` means the account is out of credit; waiting
        a minute and retrying can never succeed, so it must be classified as
        a deterministic billing failure rather than a transient rate limit.
        """
        result = classify_api_error(429, QUOTA_429_BODY)
        assert result.reason == "billing"
        assert result.category is ErrorCategory.DETERMINISTIC
        assert not result.retryable
        assert result.should_fallback

    def test_overloaded_429_still_transient(self):
        result = classify_api_error(429, "server is overloaded")
        assert result.reason == "overloaded"
        assert result.category is ErrorCategory.TRANSIENT


class _StubProvider(LLMProvider):
    """Provider double that returns a canned error a fixed number of times."""

    _CHAT_RETRY_DELAYS = (0, 0)

    def __init__(self, response_factory):
        super().__init__("test-key")
        self._response_factory = response_factory
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, **kwargs) -> LLMResponse:
        self.calls += 1
        return self._response_factory()


def _error_response(content: str, status: int, error_type: ErrorType) -> LLMResponse:
    return LLMResponse(
        content=content,
        finish_reason="error",
        error_type=error_type,
        status_code=status,
    )


class TestProviderRetryPolicy:
    @pytest.mark.asyncio
    async def test_rate_limit_is_not_retried_on_the_same_model(self):
        provider = _StubProvider(
            lambda: _error_response(TPM_429_BODY, 429, ErrorType.TRANSIENT)
        )

        response = await provider.chat_with_retry(
            messages=[{"role": "user", "content": "hi"}]
        )

        assert provider.calls == 1, (
            "a throttled model must not be re-hit inside the same window; "
            "the fallback chain rotates instead"
        )
        assert response.finish_reason == "error"
        assert response.status_code == 429

    @pytest.mark.asyncio
    async def test_server_error_still_retried(self):
        provider = _StubProvider(
            lambda: _error_response("internal server error", 500, ErrorType.TRANSIENT)
        )

        await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

        # initial attempt + one per retry delay
        assert provider.calls == 1 + len(LLMProvider._CHAT_RETRY_DELAYS)

    @pytest.mark.asyncio
    async def test_stream_rate_limit_is_not_retried(self):
        provider = _StubProvider(
            lambda: _error_response(TPM_429_BODY, 429, ErrorType.TRANSIENT)
        )

        async def _chat_stream(**kwargs):
            return await provider.chat(**kwargs)

        provider.chat_stream = _chat_stream  # type: ignore[assignment]

        response = await provider.chat_stream_with_retry(
            messages=[{"role": "user", "content": "hi"}]
        )

        assert provider.calls == 1
        assert response.finish_reason == "error"


def _manager(chain: list[str]) -> FallbackManager:
    config = Config(
        providers=ProvidersConfig(
            custom=ProviderConfig(
                api_key="sk-test",
                models=[
                    ModelConfig(id="m1", name="m1"),
                    ModelConfig(id="m2", name="m2"),
                ],
            ),
        ),
        agents=AgentsConfig(defaults=AgentDefaults(model_chain=chain)),
    )
    return FallbackManager(config)


class TestChainLevelRateLimitRecovery:
    @pytest.mark.asyncio
    async def test_all_models_throttled_waits_then_retries_once(self, monkeypatch):
        mgr = _manager(["custom/m1", "custom/m2"])
        mgr._get_or_create_provider = lambda *a, **kw: object()
        sleeps: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

        calls: list[str] = []

        async def caller(provider, model_config, mt, temp, reasoning, msgs):
            calls.append(model_config.name)
            return _error_response(TPM_429_BODY, 429, ErrorType.TRANSIENT)

        with pytest.raises(AllModelsFailedError):
            await mgr._try_chain(
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
                tool_choice=None,
                max_tokens=None,
                temperature=None,
                caller=caller,
            )

        # one pass over the chain, one window wait, one final pass
        assert calls == ["m1", "m2", "m1", "m2"]
        assert sleeps == [RATE_LIMIT_WINDOW_SECONDS]

    @pytest.mark.asyncio
    async def test_chain_recovers_after_the_window(self, monkeypatch):
        mgr = _manager(["custom/m1"])
        mgr._get_or_create_provider = lambda *a, **kw: object()

        async def _fake_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

        calls = {"n": 0}

        async def caller(provider, model_config, mt, temp, reasoning, msgs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _error_response(TPM_429_BODY, 429, ErrorType.TRANSIENT)
            return LLMResponse(content="ok after window", finish_reason="stop")

        response, attempts = await mgr._try_chain(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            tool_choice=None,
            max_tokens=None,
            temperature=None,
            caller=caller,
        )

        assert response.content == "ok after window"
        assert calls["n"] == 2
        assert attempts[-1].success is True

    @pytest.mark.asyncio
    async def test_retry_after_header_overrides_default_window(self, monkeypatch):
        """The server's Retry-After is authoritative when it sends one."""
        mgr = _manager(["custom/m1"])
        mgr._get_or_create_provider = lambda *a, **kw: object()
        sleeps: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

        async def caller(provider, model_config, mt, temp, reasoning, msgs):
            return LLMResponse(
                content=TPM_429_BODY,
                finish_reason="error",
                error_type=ErrorType.TRANSIENT,
                status_code=429,
                retry_after=7.0,
            )

        with pytest.raises(AllModelsFailedError):
            await mgr._try_chain(
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
                tool_choice=None,
                max_tokens=None,
                temperature=None,
                caller=caller,
            )

        assert sleeps == [7.0]

    @pytest.mark.asyncio
    async def test_non_rate_limit_failure_does_not_wait(self, monkeypatch):
        mgr = _manager(["custom/m1"])
        mgr._get_or_create_provider = lambda *a, **kw: object()
        sleeps: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

        async def caller(provider, model_config, mt, temp, reasoning, msgs):
            return _error_response("invalid request body", 400, ErrorType.UNAVAILABLE)

        with pytest.raises(AllModelsFailedError):
            await mgr._try_chain(
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
                tool_choice=None,
                max_tokens=None,
                temperature=None,
                caller=caller,
            )

        assert sleeps == []


class TestSdkRetryPolicy:
    """The SDK must not run its own retry loop on top of ours.

    ``AsyncOpenAI`` / ``AsyncAnthropic`` default to ``max_retries=2`` and
    retry 429s after ~0.5s. Inside a TPM/RPM window those retries are
    guaranteed to fail, and each one re-sends the full prompt. Combined with
    ``chat_with_retry``'s own attempts, one logical call became up to nine
    HTTP requests — which is what kept the account pinned at the limit.
    """

    def test_openai_compat_client_has_no_sdk_retries(self):
        from markbot.providers.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider(api_key="k", api_base="http://relay.test/v1")
        assert provider._client.max_retries == 0

    def test_anthropic_client_has_no_sdk_retries(self):
        from markbot.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="k")
        assert provider._client.max_retries == 0


class TestUserScenario:
    """The exact shape of the reported failure, end to end through the chain."""

    @pytest.mark.asyncio
    async def test_second_model_serves_when_first_is_rate_limited(self):
        mgr = _manager(["custom/m1", "custom/m2"])
        mgr._get_or_create_provider = lambda *a, **kw: object()
        calls: list[str] = []

        async def caller(provider, model_config, mt, temp, reasoning, msgs):
            calls.append(model_config.name)
            if model_config.name == "m1":
                return _error_response(TPM_429_BODY, 429, ErrorType.TRANSIENT)
            return LLMResponse(content="上海要降温了", finish_reason="stop")

        response, attempts = await mgr._try_chain(
            messages=[{"role": "user", "content": "后面是不是要降温了？"}],
            tools=None,
            tool_choice=None,
            max_tokens=None,
            temperature=None,
            caller=caller,
        )

        # The throttled model was hit exactly once, then the chain rotated.
        assert calls == ["m1", "m2"]
        assert response.content == "上海要降温了"
        assert attempts[0].success is False
        assert attempts[-1].success is True


class TestErrorSurfacing:
    @pytest.mark.asyncio
    async def test_chain_error_carries_the_upstream_message(self, monkeypatch):
        mgr = _manager(["custom/m1", "custom/m2"])
        mgr._get_or_create_provider = lambda *a, **kw: object()

        async def _fake_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

        async def caller(provider, model_config, mt, temp, reasoning, msgs):
            return _error_response(TPM_429_BODY, 429, ErrorType.TRANSIENT)

        with pytest.raises(AllModelsFailedError) as excinfo:
            await mgr._try_chain(
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
                tool_choice=None,
                max_tokens=None,
                temperature=None,
                caller=caller,
            )

        err = excinfo.value
        assert "inference exceeds tpm/rpm limit" in str(err)
        summary = err.summary
        assert "custom/m1" in summary
        assert "custom/m2" in summary
        assert "inference exceeds tpm/rpm limit" in summary
