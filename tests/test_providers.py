"""Tests for markbot.providers module (base, registry, fallback)."""

import pytest

from markbot.providers.base import LLMResponse, ToolCallRequest
from markbot.providers.fallback import (
    CircuitState,
    FallbackAttempt,
    FallbackManager,
    AllModelsFailedError,
)
from markbot.providers.registry import ProviderSpec, PROVIDERS, find_by_name


class TestToolCallRequest:
    def test_basic_request(self):
        tcr = ToolCallRequest(id="call_1", name="read_file", arguments={"path": "/tmp/f"})
        assert tcr.id == "call_1"
        assert tcr.name == "read_file"
        assert tcr.arguments == {"path": "/tmp/f"}

    def test_to_openai_tool_call(self):
        tcr = ToolCallRequest(id="call_1", name="read_file", arguments={"path": "/tmp/f"})
        result = tcr.to_openai_tool_call()
        assert result["id"] == "call_1"
        assert result["type"] == "function"
        assert result["function"]["name"] == "read_file"
        assert "arguments" in result["function"]


class TestLLMResponse:
    def test_basic_response(self):
        r = LLMResponse(content="Hello!")
        assert r.content == "Hello!"
        assert r.tool_calls == []
        assert r.finish_reason == "stop"
        assert r.has_tool_calls is False

    def test_response_with_tool_calls(self):
        r = LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(id="1", name="read", arguments={})],
            finish_reason="tool_calls",
        )
        assert r.has_tool_calls is True

    def test_response_with_usage(self):
        r = LLMResponse(content="Hi", usage={"input_tokens": 10, "output_tokens": 5})
        assert r.usage["input_tokens"] == 10

    def test_reasoning_content(self):
        r = LLMResponse(content="answer", reasoning_content="thinking...")
        assert r.reasoning_content == "thinking..."


class TestCircuitState:
    def test_defaults(self):
        cs = CircuitState()
        assert cs.failure_count == 0
        assert cs.state == "closed"
        assert cs.is_open is False

    def test_open_state(self):
        cs = CircuitState(state="open")
        assert cs.is_open is True


class TestFallbackAttempt:
    def test_defaults(self):
        fa = FallbackAttempt(model_ref="anthropic/claude-sonnet-4-5")
        assert fa.success is False
        assert fa.error is None
        assert fa.circuit_skipped is False


class TestFallbackManager:
    def test_is_retryable_error_timeout(self):
        assert FallbackManager._is_retryable_error("connection timeout") is True

    def test_is_retryable_error_rate_limit(self):
        assert FallbackManager._is_retryable_error("rate limit exceeded") is True

    def test_is_retryable_error_503(self):
        assert FallbackManager._is_retryable_error("503 service unavailable") is True

    def test_is_not_retryable_error(self):
        assert FallbackManager._is_retryable_error("invalid api key") is False

    def test_is_model_unavailable_error(self):
        mgr = FallbackManager.__new__(FallbackManager)
        assert mgr._is_model_unavailable_error("402 insufficient balance") is True
        assert mgr._is_model_unavailable_error("401 unauthorized") is True
        assert mgr._is_model_unavailable_error("model not found") is True
        assert mgr._is_model_unavailable_error("timeout") is False

    def test_circuit_breaker_flow(self):
        mgr = FallbackManager.__new__(FallbackManager)
        mgr._circuits = {}
        mgr._circuit_threshold = 3
        mgr._circuit_cooldown = 60.0

        assert mgr._check_circuit("test") is True
        mgr._record_failure("test")
        mgr._record_failure("test")
        mgr._record_failure("test")
        assert mgr._check_circuit("test") is False

        mgr._record_success("test")
        assert mgr._check_circuit("test") is True


class TestProviderSpec:
    def test_basic_spec(self):
        spec = ProviderSpec(name="test", keywords=("test",), env_key="TEST_KEY")
        assert spec.name == "test"
        assert spec.label == "Test"

    def test_custom_display_name(self):
        spec = ProviderSpec(
            name="openai", keywords=("openai",), env_key="OPENAI_API_KEY",
            display_name="OpenAI",
        )
        assert spec.label == "OpenAI"

    def test_gateway_flag(self):
        spec = ProviderSpec(
            name="openrouter", keywords=("openrouter",), env_key="OPENROUTER_API_KEY",
            is_gateway=True,
        )
        assert spec.is_gateway is True


class TestProviderRegistry:
    def test_providers_not_empty(self):
        assert len(PROVIDERS) > 0

    def test_find_by_name(self):
        result = find_by_name("anthropic")
        assert result is not None
        assert result.name == "anthropic"

    def test_find_by_name_not_found(self):
        result = find_by_name("nonexistent_provider")
        assert result is None

    def test_custom_provider_exists(self):
        result = find_by_name("custom")
        assert result is not None
        assert result.is_direct is True

    def test_anthropic_provider(self):
        result = find_by_name("anthropic")
        assert result is not None
        assert result.backend == "anthropic"

    def test_openai_provider(self):
        result = find_by_name("openai")
        assert result is not None
