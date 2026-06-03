"""Tests for fallback manager and circuit breaker."""


from markbot.config.schema import (
    AgentDefaults,
    AgentsConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProvidersConfig,
)
from markbot.providers.base import LLMResponse, ToolCallRequest
from markbot.providers.fallback import (
    CircuitState,
    FallbackManager,
)


class TestCircuitState:
    def test_default_state_is_closed(self):
        cs = CircuitState()
        assert cs.state == "closed"
        assert cs.is_open is False

    def test_open_state(self):
        cs = CircuitState(state="open")
        assert cs.is_open is True


class TestFallbackManager:
    def _make_config(self, chain=None):
        return Config(
            providers=ProvidersConfig(
                anthropic=ProviderConfig(
                    api_key="sk-test",
                    models=[ModelConfig(id="sonnet", name="claude-sonnet-4-5")],
                ),
                openai=ProviderConfig(
                    api_key="sk-test",
                    models=[ModelConfig(id="gpt4", name="gpt-4o")],
                ),
            ),
            agents=AgentsConfig(
                defaults=AgentDefaults(
                    model_chain=chain or ["anthropic/sonnet", "openai/gpt4"]
                )
            ),
        )

    def test_circuit_threshold_default(self):
        assert FallbackManager.DEFAULT_CIRCUIT_THRESHOLD == 6

    def test_circuit_cooldown_default(self):
        assert FallbackManager.DEFAULT_CIRCUIT_COOLDOWN == 60.0


class TestLLMResponse:
    def test_has_tool_calls_true(self):
        resp = LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="1", name="read", arguments={})],
        )
        assert resp.has_tool_calls is True

    def test_has_tool_calls_false(self):
        resp = LLMResponse(content="hello")
        assert resp.has_tool_calls is False

    def test_default_finish_reason(self):
        resp = LLMResponse(content="hello")
        assert resp.finish_reason == "stop"

    def test_tool_call_serialization(self):
        tc = ToolCallRequest(id="1", name="read_file", arguments={"path": "/tmp/test"})
        serialized = tc.to_openai_tool_call()
        assert serialized["type"] == "function"
        assert serialized["function"]["name"] == "read_file"
        assert '"path"' in serialized["function"]["arguments"]
