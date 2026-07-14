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
from markbot.providers.errors import ErrorType
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


# ---------------------------------------------------------------------------
# Circuit breaker isolation: the circuit must be keyed by the full
# model_ref (e.g. "custom/deepseek-v4-flash"), NOT by provider name.
# Keying by provider name makes every model on the same provider share
# one circuit — a run of failures on one model takes down every other
# model on that provider, defeating the fallback chain's redundancy.
# ---------------------------------------------------------------------------


class TestCircuitBreakerKeyedByModelRef:
    def _make_manager(self):
        return FallbackManager(
            Config(
                providers=ProvidersConfig(
                    anthropic=ProviderConfig(
                        api_key="sk-test",
                        models=[ModelConfig(id="sonnet", name="claude-sonnet-4-5")],
                    ),
                ),
                agents=AgentsConfig(
                    defaults=AgentDefaults(
                        model_chain=["anthropic/sonnet"]
                    )
                ),
            )
        )

    def test_failure_on_one_model_does_not_open_circuit_for_another(self):
        mgr = self._make_manager()
        for _ in range(FallbackManager.DEFAULT_CIRCUIT_THRESHOLD):
            mgr._record_failure("custom/a")
        # custom/a has enough failures to open
        assert mgr._get_circuit("custom/a").is_open is True
        assert mgr._check_circuit("custom/a") is False
        # custom/b is a different model_ref — circuit must be independent
        assert mgr._check_circuit("custom/b") is True

    def test_success_on_one_model_does_not_close_circuit_for_another(self):
        mgr = self._make_manager()
        for _ in range(FallbackManager.DEFAULT_CIRCUIT_THRESHOLD):
            mgr._record_failure("custom/a")
        assert mgr._get_circuit("custom/a").is_open is True
        # Recording success on custom/b must NOT reset custom/a's circuit
        mgr._record_success("custom/b")
        assert mgr._get_circuit("custom/a").is_open is True

    def test_same_provider_different_models_have_independent_circuits(self):
        """The real-world case: custom/deepseek, custom/mimo, custom/minimax
        all share provider 'custom' but must have separate circuits."""
        mgr = self._make_manager()
        for _ in range(FallbackManager.DEFAULT_CIRCUIT_THRESHOLD):
            mgr._record_failure("custom/deepseek-v4-flash")
        assert mgr._check_circuit("custom/deepseek-v4-flash") is False
        assert mgr._check_circuit("custom/mimo-v2.5-pro") is True
        assert mgr._check_circuit("custom/minimax-m2.5-free") is True


# ---------------------------------------------------------------------------
# content_filter must not trip the circuit breaker. content_filter is a
# content-specific refusal (the model is healthy, it just refused THIS
# request), so recording it as a failure would open the circuit on a
# healthy provider after 6 refusals.
# ---------------------------------------------------------------------------


class TestContentFilterDoesNotTripCircuit:
    def _make_manager(self):
        return FallbackManager(
            Config(
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
                        model_chain=["anthropic/sonnet", "openai/gpt4"]
                    )
                ),
            )
        )

    async def test_content_filter_does_not_record_failure(self):
        mgr = self._make_manager()
        # Stub out provider creation — the mock caller ignores the provider.
        mgr._get_or_create_provider = lambda pc, pn: object()

        call_count = 0

        async def mock_caller(provider, model_config, mt, temp, reasoning, msgs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return LLMResponse(
                    content="",
                    finish_reason="content_filter",
                    error_type=ErrorType.CONTENT,
                )
            return LLMResponse(content="ok", finish_reason="stop")

        response, attempts = await mgr._try_chain(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            tool_choice=None,
            max_tokens=None,
            temperature=None,
            caller=mock_caller,
        )

        # First model hit content_filter, second succeeded
        assert attempts[0].error == "content_filter"
        assert attempts[1].success is True
        # The first model's circuit must NOT have recorded a failure
        circuit = mgr._get_circuit("anthropic/sonnet")
        assert circuit.failure_count == 0
        assert circuit.state == "closed"

    async def test_repeated_content_filter_does_not_open_circuit(self):
        """Even after many content_filter hits, the circuit stays closed —
        the provider is healthy, it just keeps refusing this content."""
        mgr = self._make_manager()
        mgr._get_or_create_provider = lambda pc, pn: object()

        async def mock_caller(provider, model_config, mt, temp, reasoning, msgs):
            # Both models always refuse
            return LLMResponse(
                content="",
                finish_reason="content_filter",
                error_type=ErrorType.CONTENT,
            )

        # Run the chain several times — each run hits content_filter on
        # both models. None of these should trip the circuit.
        for _ in range(FallbackManager.DEFAULT_CIRCUIT_THRESHOLD + 2):
            try:
                await mgr._try_chain(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=None,
                    tool_choice=None,
                    max_tokens=None,
                    temperature=None,
                    caller=mock_caller,
                )
            except Exception:
                pass  # AllModelsFailedError expected

        circuit = mgr._get_circuit("anthropic/sonnet")
        assert circuit.failure_count == 0
        assert circuit.state == "closed"

    async def test_content_filter_releases_half_open_probe(self):
        """A content_filter during a half-open probe must release the probe.

        Without the release, _check_circuit would keep seeing 'probe in
        flight' on every subsequent call and permanently skip the model —
        a single content_filter during a half-open probe would lock the
        model out of the chain forever.
        """
        mgr = self._make_manager()
        mgr._get_or_create_provider = lambda pc, pn: object()

        # Put anthropic/sonnet into half-open, simulating the state
        # after 6 failures + cooldown elapsed. _check_circuit will add
        # the probe to _half_open_probes when it allows the request.
        circuit = mgr._get_circuit("anthropic/sonnet")
        circuit.state = "half-open"

        async def mock_caller(provider, model_config, mt, temp, reasoning, msgs):
            # First model: content_filter; second model: success
            if model_config.name == "claude-sonnet-4-5":
                return LLMResponse(
                    content="",
                    finish_reason="content_filter",
                    error_type=ErrorType.CONTENT,
                )
            return LLMResponse(content="ok", finish_reason="stop")

        await mgr._try_chain(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            tool_choice=None,
            max_tokens=None,
            temperature=None,
            caller=mock_caller,
        )

        # Probe must be released so the next request can probe this model
        assert "anthropic/sonnet" not in mgr._half_open_probes
        # Circuit stays half-open — content_filter is neither success nor failure
        assert circuit.state == "half-open"

        # Second call: the model must NOT be skipped (probe was released)
        _, attempts = await mgr._try_chain(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            tool_choice=None,
            max_tokens=None,
            temperature=None,
            caller=mock_caller,
        )
        assert attempts[0].circuit_skipped is False
        assert attempts[0].error == "content_filter"
