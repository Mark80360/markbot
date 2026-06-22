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


# ---------------------------------------------------------------------------
# Per-model capability-aware message adaptation (debug session
# markbot-multimodal-chain-fail).  These tests pin down:
#  * non-vision models receive the image-stripped message list
#  * vision-capable models receive the original list unchanged
#  * the original list is never mutated
# ---------------------------------------------------------------------------


def _multimodal_messages() -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what's on the page?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAA"},
                    "_meta": {"path": "/tmp/screen.png"},
                },
            ],
        }
    ]


class TestAdaptMessagesForModel:
    def test_vision_capable_passes_through_unchanged(self):
        msgs = _multimodal_messages()
        model = ModelConfig(
            id="vl", name="vl-1", capabilities=["text", "image"],
        )
        out = FallbackManager._adapt_messages_for_model(msgs, "p", model)
        assert out is msgs  # exact same list, no copy

    def test_text_only_strips_image(self):
        msgs = _multimodal_messages()
        model = ModelConfig(id="t", name="t-1")  # default capabilities = ["text"]
        out = FallbackManager._adapt_messages_for_model(msgs, "p", model)
        assert out is not msgs
        # Original text block is preserved; image_url becomes a text
        # placeholder. The order is preserved.
        blocks = out[0]["content"]
        assert len(blocks) == 2
        assert blocks[0] == {"type": "text", "text": "what's on the page?"}
        assert blocks[1]["type"] == "text"
        assert "image" in blocks[1]["text"].lower()
        assert "/tmp/screen.png" in blocks[1]["text"]
        # No more image_url blocks anywhere
        assert all(b.get("type") != "image_url" for b in blocks)

    def test_text_only_with_no_image_returns_input(self):
        msgs = [{"role": "user", "content": "hello"}]
        model = ModelConfig(id="t", name="t-1")
        out = FallbackManager._adapt_messages_for_model(msgs, "p", model)
        assert out is msgs

    def test_text_only_does_not_mutate_input(self):
        msgs = _multimodal_messages()
        original_first = msgs[0]["content"][1]
        model = ModelConfig(id="t", name="t-1")
        FallbackManager._adapt_messages_for_model(msgs, "p", model)
        # input is unchanged
        assert msgs[0]["content"][1] is original_first
        assert msgs[0]["content"][1]["type"] == "image_url"

    def test_empty_messages_returns_empty(self):
        model = ModelConfig(id="t", name="t-1")
        assert FallbackManager._adapt_messages_for_model([], "p", model) == []

    def test_model_config_none_falls_back_to_strip(self):
        """If we somehow have no ModelConfig, default to text-only stripping."""
        msgs = _multimodal_messages()
        out = FallbackManager._adapt_messages_for_model(msgs, "p", None)
        assert out is not msgs
        assert out[0]["content"][0]["type"] == "text"
