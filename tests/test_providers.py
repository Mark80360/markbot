"""Tests for markbot.providers module (base, registry, fallback)."""


from markbot.providers.base import LLMResponse, ToolCallRequest
from markbot.providers.errors import ErrorType, classify_error
from markbot.providers.fallback import (
    CircuitState,
    FallbackAttempt,
    FallbackManager,
)
from markbot.providers.registry import (
    _OMIT_TEMPERATURE,
    PROVIDERS,
    AnthropicSpec,
    CustomSpec,
    DashScopeSpec,
    DeepSeekSpec,
    GeminiSpec,
    MoonshotSpec,
    OpenRouterSpec,
    ProviderSpec,
    find_by_name,
)


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


# ---------------------------------------------------------------------------
# CustomSpec thinking-type quirks (debug session markbot-multimodal-chain-fail)
# ---------------------------------------------------------------------------


class TestCustomSpecThinkingType:
    def _custom(self) -> CustomSpec:
        # Re-derive the singleton from PROVIDERS so we exercise the real spec.
        for spec in PROVIDERS:
            if isinstance(spec, CustomSpec):
                return spec
        raise AssertionError("CustomSpec missing from PROVIDERS")

    def test_minimax_uses_adaptive_type(self):
        spec = self._custom()
        reasoning = {"enabled": True, "effort": "medium"}
        eb, tl = spec.build_api_kwargs_extras(
            reasoning_config=reasoning, model="MiniMax-M3",
        )
        assert eb.get("thinking") == {"type": "adaptive"}
        # reasoning_effort still surfaced at the top level
        assert tl.get("reasoning_effort") == "medium"

    def test_minimax_disabled_still_uses_disabled_type(self):
        spec = self._custom()
        eb, _tl = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "medium"},
            model="MiniMax-M3",
        )
        assert eb.get("thinking") == {"type": "disabled"}

    def test_deepseek_keeps_legacy_enabled_type(self):
        spec = self._custom()
        # deepseek-v4 family is the "default" behavior — preserves the
        # historical "enabled" payload that the upstream actually accepts.
        eb, _ = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            model="custom/deepseek-v4-flash-free",
        )
        assert eb.get("thinking") == {"type": "enabled"}

    def test_kimi_uses_legacy_enabled_type(self):
        spec = self._custom()
        eb, _ = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "low"},
            model="kimi-k2.5",
        )
        assert eb.get("thinking") == {"type": "enabled"}

    def test_non_thinking_model_emits_nothing(self):
        spec = self._custom()
        # gpt-3.5-turbo isn't in the markers — spec should not inject
        # any thinking/reasoning config.
        eb, tl = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            model="gpt-3.5-turbo",
        )
        assert "thinking" not in eb
        assert "reasoning_effort" not in tl


class TestUsesAdaptiveThinking:
    def test_minimax_m3(self):
        assert CustomSpec._uses_adaptive_thinking("MiniMax-M3") is True

    def test_minimax_m2(self):
        assert CustomSpec._uses_adaptive_thinking("minimax-m2.5") is True

    def test_deepseek(self):
        assert CustomSpec._uses_adaptive_thinking("deepseek-v4") is False

    def test_none(self):
        assert CustomSpec._uses_adaptive_thinking(None) is False

    def test_empty(self):
        assert CustomSpec._uses_adaptive_thinking("") is False


class TestModelConfigCapabilities:
    """Pydantic-level checks on the new ``capabilities`` field."""

    def _cfg(self, **kwargs):
        from markbot.config.schema import ModelConfig
        defaults = {"id": "x", "name": "x"}
        defaults.update(kwargs)
        return ModelConfig(**defaults)

    def test_default_is_text_only(self):
        m = self._cfg()
        assert m.capabilities == ["text"]
        assert m.has_capability("text") is True
        assert m.has_capability("image") is False

    def test_explicit_capabilities(self):
        m = self._cfg(capabilities=["text", "image"])
        assert m.has_capability("image") is True
        assert m.has_capability("IMAGE") is True  # case-insensitive

    def test_string_input_is_normalized(self):
        # Comma-separated string is split so YAML/JSON users can write
        # ``capabilities: "text, image, video"`` without a list.
        m = self._cfg(capabilities="text, image, video")
        assert m.capabilities == ["text", "image", "video"]

    def test_single_string_input(self):
        m = self._cfg(capabilities="image")
        assert m.capabilities == ["image"]

    def test_unknown_capability_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            self._cfg(capabilities=["text", "telepathy"])

    def test_dedup(self):
        m = self._cfg(capabilities=["text", "image", "text", "IMAGE"])
        assert m.capabilities == ["text", "image"]


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
    def test_classify_timeout_is_transient(self):
        assert classify_error(None, "connection timeout") == ErrorType.TRANSIENT

    def test_classify_rate_limit_is_transient(self):
        assert classify_error(None, "rate limit exceeded") == ErrorType.TRANSIENT

    def test_classify_503_is_transient(self):
        assert classify_error(503, "service unavailable") == ErrorType.TRANSIENT

    def test_classify_invalid_api_key_is_unavailable(self):
        assert classify_error(None, "invalid api key") == ErrorType.UNAVAILABLE

    def test_classify_402_is_unavailable(self):
        assert classify_error(402, "insufficient balance") == ErrorType.UNAVAILABLE

    def test_classify_401_is_unavailable(self):
        assert classify_error(401, "unauthorized") == ErrorType.UNAVAILABLE

    def test_classify_model_not_found_is_unavailable(self):
        assert classify_error(None, "model not found") == ErrorType.UNAVAILABLE

    def test_classify_unknown_message_is_unknown(self):
        assert classify_error(None, "totally unrecognised error") == ErrorType.UNKNOWN

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

    def test_get_hostname_from_base_url(self):
        spec = ProviderSpec(
            name="test", keywords=("test",), env_key="TEST_KEY",
            default_api_base="https://api.example.com/v1",
        )
        assert spec.get_hostname() == "api.example.com"

    def test_get_hostname_explicit(self):
        spec = ProviderSpec(
            name="test", keywords=("test",), env_key="TEST_KEY",
            hostname="custom.host.com",
            default_api_base="https://api.example.com/v1",
        )
        assert spec.get_hostname() == "custom.host.com"

    def test_get_hostname_empty(self):
        spec = ProviderSpec(name="test", keywords=("test",), env_key="TEST_KEY")
        assert spec.get_hostname() == ""

    def test_default_hooks(self):
        spec = ProviderSpec(name="test", keywords=("test",), env_key="TEST_KEY")
        msgs = [{"role": "user", "content": "hi"}]
        assert spec.prepare_messages(msgs) is msgs
        assert spec.build_extra_body() == {}
        assert spec.build_api_kwargs_extras() == ({}, {})

    def test_new_metadata_fields(self):
        spec = ProviderSpec(
            name="test", keywords=("test",), env_key="TEST_KEY",
            aliases=("t1", "t2"),
            description="Test provider",
            signup_url="https://example.com/",
            fallback_models=("model-a", "model-b"),
            default_aux_model="model-cheap",
            auth_type="api_key",
            supports_health_check=True,
        )
        assert spec.aliases == ("t1", "t2")
        assert spec.description == "Test provider"
        assert spec.signup_url == "https://example.com/"
        assert spec.fallback_models == ("model-a", "model-b")
        assert spec.default_aux_model == "model-cheap"
        assert spec.auth_type == "api_key"
        assert spec.supports_health_check is True

    def test_fixed_temperature_omit(self):
        spec = ProviderSpec(
            name="test", keywords=("test",), env_key="TEST_KEY",
            fixed_temperature=_OMIT_TEMPERATURE,
        )
        assert spec.fixed_temperature is _OMIT_TEMPERATURE

    def test_fixed_temperature_value(self):
        spec = ProviderSpec(
            name="test", keywords=("test",), env_key="TEST_KEY",
            fixed_temperature=1.0,
        )
        assert spec.fixed_temperature == 1.0


class TestDeepSeekSpec:
    def test_thinking_model_v4(self):
        spec = DeepSeekSpec(name="deepseek", keywords=("deepseek",), env_key="DEEPSEEK_API_KEY")
        assert spec.model_supports_thinking("deepseek-v4-pro") is True
        assert spec.model_supports_thinking("deepseek-v4-flash") is True

    def test_non_thinking_model_v3(self):
        spec = DeepSeekSpec(name="deepseek", keywords=("deepseek",), env_key="DEEPSEEK_API_KEY")
        assert spec.model_supports_thinking("deepseek-chat") is False
        assert spec.model_supports_thinking("deepseek-v3") is False

    def test_reasoner_model(self):
        spec = DeepSeekSpec(name="deepseek", keywords=("deepseek",), env_key="DEEPSEEK_API_KEY")
        assert spec.model_supports_thinking("deepseek-reasoner") is True

    def test_build_api_kwargs_extras_thinking_enabled(self):
        spec = DeepSeekSpec(name="deepseek", keywords=("deepseek",), env_key="DEEPSEEK_API_KEY")
        eb, tl = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="deepseek-v4-pro",
        )
        assert eb["thinking"] == {"type": "enabled"}
        assert tl["reasoning_effort"] == "high"

    def test_build_api_kwargs_extras_thinking_disabled(self):
        spec = DeepSeekSpec(name="deepseek", keywords=("deepseek",), env_key="DEEPSEEK_API_KEY")
        eb, tl = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="deepseek-v4-pro",
        )
        assert eb["thinking"] == {"type": "disabled"}
        assert tl == {}

    def test_build_api_kwargs_extras_non_thinking_model(self):
        spec = DeepSeekSpec(name="deepseek", keywords=("deepseek",), env_key="DEEPSEEK_API_KEY")
        eb, tl = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="deepseek-chat",
        )
        assert eb == {}
        assert tl == {}

    def test_effort_xhigh_maps_to_max(self):
        spec = DeepSeekSpec(name="deepseek", keywords=("deepseek",), env_key="DEEPSEEK_API_KEY")
        eb, tl = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="deepseek-v4-pro",
        )
        assert tl["reasoning_effort"] == "max"


class TestMoonshotSpec:
    def test_default_thinking_enabled(self):
        spec = MoonshotSpec(name="moonshot", keywords=("moonshot",), env_key="MOONSHOT_API_KEY")
        eb, tl = spec.build_api_kwargs_extras()
        assert eb["thinking"] == {"type": "enabled"}
        assert tl["reasoning_effort"] == "medium"

    def test_thinking_disabled(self):
        spec = MoonshotSpec(name="moonshot", keywords=("moonshot",), env_key="MOONSHOT_API_KEY")
        eb, tl = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
        )
        assert eb["thinking"] == {"type": "disabled"}
        assert tl == {}

    def test_custom_effort(self):
        spec = MoonshotSpec(name="moonshot", keywords=("moonshot",), env_key="MOONSHOT_API_KEY")
        eb, tl = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "low"},
        )
        assert tl["reasoning_effort"] == "low"

    def test_omit_temperature(self):
        spec = MoonshotSpec(
            name="moonshot", keywords=("moonshot",), env_key="MOONSHOT_API_KEY",
            fixed_temperature=_OMIT_TEMPERATURE,
        )
        assert spec.fixed_temperature is _OMIT_TEMPERATURE


class TestOpenRouterSpec:
    def test_build_extra_body_with_preferences(self):
        spec = OpenRouterSpec(
            name="openrouter", keywords=("openrouter",), env_key="OPENROUTER_API_KEY",
        )
        body = spec.build_extra_body(provider_preferences={"allow_fallback": True})
        assert body["provider"] == {"allow_fallback": True}

    def test_build_extra_body_without_preferences(self):
        spec = OpenRouterSpec(
            name="openrouter", keywords=("openrouter",), env_key="OPENROUTER_API_KEY",
        )
        body = spec.build_extra_body()
        assert body == {}

    def test_reasoning_passthrough(self):
        spec = OpenRouterSpec(
            name="openrouter", keywords=("openrouter",), env_key="OPENROUTER_API_KEY",
        )
        eb, tl = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            supports_reasoning=True,
        )
        assert eb["reasoning"]["effort"] == "high"

    def test_xai_session_affinity(self):
        spec = OpenRouterSpec(
            name="openrouter", keywords=("openrouter",), env_key="OPENROUTER_API_KEY",
        )
        eb, tl = spec.build_api_kwargs_extras(
            model="x-ai/grok-3",
            session_id="sess-123",
        )
        assert "extra_headers" in tl
        assert tl["extra_headers"]["x-grok-conv-id"] == "sess-123"


class TestGeminiSpec:
    def test_thinking_config_enabled(self):
        spec = GeminiSpec(name="gemini", keywords=("gemini",), env_key="GEMINI_API_KEY")
        body = spec.build_extra_body(
            reasoning_config={"enabled": True, "effort": "high"},
        )
        assert "google" in body
        assert body["google"]["thinking_config"]["thinking_budget"] == 65536

    def test_thinking_config_disabled(self):
        spec = GeminiSpec(name="gemini", keywords=("gemini",), env_key="GEMINI_API_KEY")
        body = spec.build_extra_body(
            reasoning_config={"enabled": False},
        )
        assert body == {}

    def test_no_reasoning_config(self):
        spec = GeminiSpec(name="gemini", keywords=("gemini",), env_key="GEMINI_API_KEY")
        body = spec.build_extra_body()
        assert body == {}


class TestCustomSpec:
    def test_think_false_on_disabled(self):
        spec = CustomSpec(name="custom", keywords=(), env_key="")
        eb, tl = spec.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "none"},
        )
        assert eb["think"] is False

    def test_ollama_num_ctx(self):
        spec = CustomSpec(name="custom", keywords=(), env_key="")
        eb, tl = spec.build_api_kwargs_extras(ollama_num_ctx=32768)
        assert eb["options"]["num_ctx"] == 32768


class TestDashScopeSpec:
    def test_prepare_messages_normalizes_string_content(self):
        spec = DashScopeSpec(name="dashscope", keywords=("qwen",), env_key="DASHSCOPE_API_KEY")
        msgs = [{"role": "user", "content": "hello"}]
        result = spec.prepare_messages(msgs)
        assert result[0]["content"] == [{"type": "text", "text": "hello"}]

    def test_prepare_messages_injects_cache_control(self):
        spec = DashScopeSpec(name="dashscope", keywords=("qwen",), env_key="DASHSCOPE_API_KEY")
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "hi"},
        ]
        result = spec.prepare_messages(msgs)
        system_content = result[0]["content"]
        assert isinstance(system_content, list)
        assert system_content[-1].get("cache_control") == {"type": "ephemeral"}

    def test_build_extra_body(self):
        spec = DashScopeSpec(name="dashscope", keywords=("qwen",), env_key="DASHSCOPE_API_KEY")
        body = spec.build_extra_body()
        assert body["vl_high_resolution_images"] is True


class TestAnthropicSpec:
    def test_fetch_models_requires_api_key(self):
        spec = AnthropicSpec(
            name="anthropic", keywords=("anthropic",), env_key="ANTHROPIC_API_KEY",
        )
        result = spec.fetch_models(api_key=None)
        assert result is None


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

    def test_find_by_alias(self):
        result = find_by_name("claude")
        assert result is not None
        assert result.name == "anthropic"

    def test_find_by_alias_kimi(self):
        result = find_by_name("kimi")
        assert result is not None
        assert result.name == "moonshot"

    def test_find_by_alias_or(self):
        result = find_by_name("or")
        assert result is not None
        assert result.name == "openrouter"

    def test_find_by_alias_grok(self):
        result = find_by_name("grok")
        assert result is not None
        assert result.name == "xai"

    def test_find_by_alias_hf(self):
        result = find_by_name("hf")
        assert result is not None
        assert result.name == "huggingface"

    def test_deepseek_is_subclass(self):
        result = find_by_name("deepseek")
        assert isinstance(result, DeepSeekSpec)

    def test_moonshot_is_subclass(self):
        result = find_by_name("moonshot")
        assert isinstance(result, MoonshotSpec)

    def test_openrouter_is_subclass(self):
        result = find_by_name("openrouter")
        assert isinstance(result, OpenRouterSpec)

    def test_gemini_is_subclass(self):
        result = find_by_name("gemini")
        assert isinstance(result, GeminiSpec)

    def test_custom_is_subclass(self):
        result = find_by_name("custom")
        assert isinstance(result, CustomSpec)

    def test_dashscope_is_subclass(self):
        result = find_by_name("dashscope")
        assert isinstance(result, DashScopeSpec)

    def test_anthropic_is_subclass(self):
        result = find_by_name("anthropic")
        assert isinstance(result, AnthropicSpec)

    def test_new_providers_exist(self):
        for name in ("xai", "nvidia", "huggingface"):
            result = find_by_name(name)
            assert result is not None, f"Provider '{name}' not found"

    def test_all_providers_have_description(self):
        for spec in PROVIDERS:
            if spec.name in ("custom", "azure_openai") or spec.is_local:
                continue
            assert spec.description, f"Provider '{spec.name}' missing description"

    def test_all_providers_have_signup_url_or_no_key(self):
        for spec in PROVIDERS:
            if not spec.env_key or spec.is_local or spec.is_direct or spec.is_oauth:
                continue
            assert spec.signup_url, f"Provider '{spec.name}' with env_key missing signup_url"

    def test_moonshot_omits_temperature(self):
        result = find_by_name("moonshot")
        assert result.fixed_temperature is _OMIT_TEMPERATURE

    def test_deepseek_has_fallback_models(self):
        result = find_by_name("deepseek")
        assert len(result.fallback_models) > 0
