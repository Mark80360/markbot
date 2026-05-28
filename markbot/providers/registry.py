"""
Provider Registry — single source of truth for LLM provider metadata.

Adding a new provider:
  1. Add a ProviderSpec (or subclass) to PROVIDERS below.
  2. Add a field to ProvidersConfig in config/schema.py.
  Done. Env vars, config matching, status display all derive from here.

For providers with non-trivial request quirks (thinking mode, reasoning
effort, extra_body fields), subclass ProviderSpec and override the hook
methods (prepare_messages, build_extra_body, build_api_kwargs_extras).

Order matters — it controls match priority and fallback. Gateways first.
Every entry writes out all fields so you can copy-paste as a template.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from pydantic.alias_generators import to_snake

_OMIT_TEMPERATURE = object()


@dataclass(frozen=True)
class ProviderSpec:
    """One LLM provider's metadata. See PROVIDERS below for real examples.

    Placeholders in env_extras values:
      {api_key}  — the user's API key
      {api_base} — api_base from config, or this spec's default_api_base

    Hooks (override in subclass for complex providers):
      prepare_messages(msgs)         — provider-specific message preprocessing
      build_extra_body(**ctx)        — provider-specific extra_body fields
      build_api_kwargs_extras(**ctx) — (extra_body_additions, top_level_kwargs)
      fetch_models(*, api_key)       — live model catalog fetch
    """

    # identity
    name: str  # config field name, e.g. "dashscope"
    keywords: tuple[str, ...]  # model-name keywords for matching (lowercase)
    env_key: str  # env var for API key, e.g. "DASHSCOPE_API_KEY"
    display_name: str = ""  # shown in `markbot status`
    aliases: tuple[str, ...] = ()  # alternative names, e.g. ("claude", "dashscope")
    description: str = ""  # one-line description for setup picker
    signup_url: str = ""  # e.g. "https://platform.deepseek.com/"

    # which provider implementation to use
    # "openai_compat" | "anthropic" | "azure_openai" | "openai_codex"
    backend: str = "openai_compat"

    # extra env vars, e.g. (("ZHIPUAI_API_KEY", "{api_key}"),)
    env_extras: tuple[tuple[str, str], ...] = ()

    # gateway / local detection
    is_gateway: bool = False  # routes any model (OpenRouter, AiHubMix)
    is_local: bool = False  # local deployment (vLLM, Ollama)
    detect_by_key_prefix: str = ""  # match api_key prefix, e.g. "sk-or-"
    detect_by_base_keyword: str = ""  # match substring in api_base URL
    default_api_base: str = ""  # OpenAI-compatible base URL for this provider

    # gateway behavior
    strip_model_prefix: bool = False  # strip "provider/" before sending to gateway

    # per-model param overrides, e.g. (("kimi-k2.5", {"temperature": 1.0}),)
    model_overrides: tuple[tuple[str, dict[str, Any]], ...] = ()

    # OAuth-based providers (e.g., OpenAI Codex) don't use API keys
    is_oauth: bool = False

    # Direct providers skip API-key validation (user supplies everything)
    is_direct: bool = False

    # Provider supports cache_control on content blocks (e.g. Anthropic prompt caching)
    supports_prompt_caching: bool = False

    # auth type: "api_key" | "oauth" | "aws_sdk" | "external_process"
    auth_type: str = "api_key"

    # hostname for URL-based provider detection (derived from default_api_base if empty)
    hostname: str = ""

    # fallback models shown in picker when live fetch fails
    fallback_models: tuple[str, ...] = ()

    # temperature: None = use caller's default, OMIT_TEMPERATURE = don't send at all
    fixed_temperature: Any = None

    # default max_tokens for this provider
    default_max_tokens: int | None = None

    # cheap model for auxiliary tasks (compression, vision, etc.)
    default_aux_model: str = ""

    # whether /models health check is meaningful
    supports_health_check: bool = True

    # explicit models endpoint (falls back to {default_api_base}/models)
    models_url: str = ""

    # default headers sent with every request
    default_headers: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        return self.display_name or self.name.title()

    def get_hostname(self) -> str:
        """Return the provider's base hostname for URL-based detection."""
        if self.hostname:
            return self.hostname
        if self.default_api_base:
            return urlparse(self.default_api_base).hostname or ""
        return ""

    # ------------------------------------------------------------------
    # Hooks — override in subclass for provider-specific quirks
    # ------------------------------------------------------------------

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Provider-specific message preprocessing. Default: pass-through."""
        return messages

    def build_extra_body(self, **context: Any) -> dict[str, Any]:
        """Provider-specific extra_body fields. Default: empty dict."""
        return {}

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Provider-specific kwargs split between extra_body and top-level.

        Returns (extra_body_additions, top_level_kwargs).
        The transport merges extra_body_additions into extra_body, and
        top_level_kwargs directly into api_kwargs.

        Default: ({}, {}).
        """
        return {}, {}

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Fetch the live model list from the provider's models endpoint.

        Returns a list of model ID strings, or None if the fetch failed.
        """
        url = (self.models_url or "").strip()
        if not url:
            if not self.default_api_base:
                return None
            url = self.default_api_base.rstrip("/") + "/models"

        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "markbot/1.0")
        for k, v in self.default_headers:
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            items = data if isinstance(data, list) else data.get("data", [])
            return [m["id"] for m in items if isinstance(m, dict) and "id" in m]
        except Exception as exc:
            logger.debug("fetch_models({}): {}", self.name, exc)
            return None


# ---------------------------------------------------------------------------
# Provider subclasses with non-trivial hooks
# ---------------------------------------------------------------------------


class DeepSeekSpec(ProviderSpec):
    """DeepSeek — extra_body.thinking + top-level reasoning_effort.

    DeepSeek V4+ and deepseek-reasoner default to thinking-mode ON when
    extra_body.thinking is unset, which causes the notorious HTTP 400
    ``reasoning_content must be passed back`` error on subsequent turns.
    This spec explicitly sets thinking to avoid that trap.
    """

    @staticmethod
    def _model_supports_thinking(model: str | None) -> bool:
        m = (model or "").strip().lower()
        if not m:
            return False
        if m.startswith("deepseek-v") and not m.startswith("deepseek-v3"):
            return True
        if m == "deepseek-reasoner":
            return True
        return False

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if not self._model_supports_thinking(model):
            return extra_body, top_level

        enabled = True
        if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is False:
            enabled = False

        extra_body["thinking"] = {"type": "enabled" if enabled else "disabled"}

        if not enabled:
            return extra_body, top_level

        if isinstance(reasoning_config, dict):
            effort = (reasoning_config.get("effort") or "").strip().lower()
            if effort in {"xhigh", "max"}:
                top_level["reasoning_effort"] = "max"
            elif effort in {"low", "medium", "high"}:
                top_level["reasoning_effort"] = effort

        return extra_body, top_level


class MoonshotSpec(ProviderSpec):
    """Moonshot/Kimi — temperature omitted, thinking + reasoning_effort.

    Kimi's API manages temperature server-side; sending it causes errors
    on K2+ models. This spec also adds thinking/reasoning support.
    """

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if not reasoning_config or not isinstance(reasoning_config, dict):
            extra_body["thinking"] = {"type": "enabled"}
            top_level["reasoning_effort"] = "medium"
            return extra_body, top_level

        enabled = reasoning_config.get("enabled", True)
        if enabled is False:
            extra_body["thinking"] = {"type": "disabled"}
            return extra_body, top_level

        extra_body["thinking"] = {"type": "enabled"}
        effort = (reasoning_config.get("effort") or "").strip().lower()
        if effort in {"low", "medium", "high"}:
            top_level["reasoning_effort"] = effort
        else:
            top_level["reasoning_effort"] = "medium"

        return extra_body, top_level


class OpenRouterSpec(ProviderSpec):
    """OpenRouter — reasoning config passthrough, provider preferences."""

    def build_extra_body(self, **context: Any) -> dict[str, Any]:
        body: dict[str, Any] = {}
        prefs = context.get("provider_preferences")
        if prefs:
            body["provider"] = prefs
        return body

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        supports_reasoning: bool = False,
        model: str | None = None,
        session_id: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        if supports_reasoning:
            if reasoning_config is not None:
                extra_body["reasoning"] = dict(reasoning_config)
            else:
                extra_body["reasoning"] = {"enabled": True, "effort": "medium"}

        extra_headers: dict[str, Any] = {}
        if session_id and model and model.startswith(("x-ai/grok-", "xai/grok-")):
            extra_headers["x-grok-conv-id"] = session_id

        return extra_body, {"extra_headers": extra_headers} if extra_headers else {}

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        url = self.models_url or (self.default_api_base.rstrip("/") + "/models")
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "markbot/1.0")
        for k, v in self.default_headers:
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            items = data if isinstance(data, list) else data.get("data", [])
            return [m["id"] for m in items if isinstance(m, dict) and "id" in m]
        except Exception as exc:
            logger.debug("fetch_models(openrouter): {}", exc)
            return None


class GeminiSpec(ProviderSpec):
    """Gemini — thinking_config translation for OpenAI-compat endpoint."""

    def build_extra_body(self, **context: Any) -> dict[str, Any]:
        model = context.get("model") or ""
        reasoning_config = context.get("reasoning_config")

        if not reasoning_config or not isinstance(reasoning_config, dict):
            return {}

        enabled = reasoning_config.get("enabled", True)
        effort = (reasoning_config.get("effort") or "medium").strip().lower()

        if not enabled:
            return {}

        budget_map = {"low": 8192, "medium": 32768, "high": 65536}
        budget = budget_map.get(effort, 32768)

        return {
            "google": {
                "thinking_config": {
                    "thinking_budget": budget,
                }
            }
        }


class CustomSpec(ProviderSpec):
    """Custom/Ollama local provider — think=false and num_ctx support."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        ollama_num_ctx: int | None = None,
        **ctx: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}

        if ollama_num_ctx:
            options = extra_body.get("options", {})
            options["num_ctx"] = ollama_num_ctx
            extra_body["options"] = options

        if reasoning_config and isinstance(reasoning_config, dict):
            _effort = (reasoning_config.get("effort") or "").strip().lower()
            _enabled = reasoning_config.get("enabled", True)
            if _effort == "none" or _enabled is False:
                extra_body["think"] = False

        return extra_body, {}

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        if not self.default_api_base:
            return None
        return ProviderSpec.fetch_models(self, api_key=api_key, timeout=timeout)


class DashScopeSpec(ProviderSpec):
    """DashScope/Qwen — vl_high_resolution, message normalization."""

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import copy
        prepared = copy.deepcopy(messages)
        if not prepared:
            return prepared

        for msg in prepared:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                normalized_parts = []
                for part in content:
                    if isinstance(part, str):
                        normalized_parts.append({"type": "text", "text": part})
                    elif isinstance(part, dict):
                        normalized_parts.append(part)
                if normalized_parts:
                    msg["content"] = normalized_parts

        for msg in prepared:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, list) and content and isinstance(content[-1], dict):
                    content[-1]["cache_control"] = {"type": "ephemeral"}
                break

        return prepared

    def build_extra_body(self, **context: Any) -> dict[str, Any]:
        return {"vl_high_resolution_images": True}


class AnthropicSpec(ProviderSpec):
    """Anthropic — native SDK with prompt caching, fetch_models via x-api-key."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        if not api_key:
            return None
        try:
            req = urllib.request.Request("https://api.anthropic.com/v1/models")
            req.add_header("x-api-key", api_key)
            req.add_header("anthropic-version", "2023-06-01")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            return [
                m["id"]
                for m in data.get("data", [])
                if isinstance(m, dict) and "id" in m
            ]
        except Exception as exc:
            logger.debug("fetch_models(anthropic): {}", exc)
            return None


# ---------------------------------------------------------------------------
# PROVIDERS — the registry. Order = priority. Copy any entry as template.
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    # === Custom (direct OpenAI-compatible endpoint) ========================
    CustomSpec(
        name="custom",
        keywords=(),
        env_key="",
        display_name="Custom",
        description="Custom OpenAI-compatible endpoint (Ollama, vLLM, etc.)",
        backend="openai_compat",
        is_direct=True,
        aliases=("ollama", "local", "vllm", "llamacpp"),
    ),

    # === Azure OpenAI (direct API calls with API version 2024-10-21) =====
    ProviderSpec(
        name="azure_openai",
        keywords=("azure", "azure-openai"),
        env_key="",
        display_name="Azure OpenAI",
        description="Microsoft Azure OpenAI — per-resource endpoint",
        signup_url="https://ai.azure.com/",
        backend="azure_openai",
        is_direct=True,
    ),
    # === Gateways (detected by api_key / api_base, not model name) =========
    OpenRouterSpec(
        name="openrouter",
        keywords=("openrouter",),
        env_key="OPENROUTER_API_KEY",
        display_name="OpenRouter",
        description="OpenRouter — unified API for 200+ models",
        signup_url="https://openrouter.ai/keys",
        backend="openai_compat",
        is_gateway=True,
        detect_by_key_prefix="sk-or-",
        detect_by_base_keyword="openrouter",
        default_api_base="https://openrouter.ai/api/v1",
        models_url="https://openrouter.ai/api/v1/models",
        supports_prompt_caching=True,
        aliases=("or",),
        fallback_models=(
            "anthropic/claude-sonnet-4-5-20250514",
            "openai/gpt-4o",
            "deepseek/deepseek-chat",
            "google/gemini-2.5-flash-preview-05-20",
        ),
    ),
    # AiHubMix: global gateway, OpenAI-compatible interface.
    ProviderSpec(
        name="aihubmix",
        keywords=("aihubmix",),
        env_key="OPENAI_API_KEY",
        display_name="AiHubMix",
        description="AiHubMix 聚合网关，提供多种模型统一接入",
        signup_url="https://aihubmix.com/",
        backend="openai_compat",
        is_gateway=True,
        detect_by_base_keyword="aihubmix",
        default_api_base="https://aihubmix.com/v1",
        strip_model_prefix=True,
    ),
    # SiliconFlow (硅基流动): OpenAI-compatible gateway
    ProviderSpec(
        name="siliconflow",
        keywords=("siliconflow",),
        env_key="OPENAI_API_KEY",
        display_name="SiliconFlow",
        description="硅基流动，国内 AI 推理云平台",
        signup_url="https://cloud.siliconflow.cn/",
        backend="openai_compat",
        is_gateway=True,
        detect_by_base_keyword="siliconflow",
        default_api_base="https://api.siliconflow.cn/v1",
    ),

    # VolcEngine (火山引擎): OpenAI-compatible gateway
    ProviderSpec(
        name="volcengine",
        keywords=("volcengine", "volces", "ark"),
        env_key="OPENAI_API_KEY",
        display_name="VolcEngine",
        description="火山引擎方舟平台，提供豆包等模型",
        signup_url="https://console.volcengine.com/ark",
        backend="openai_compat",
        is_gateway=True,
        detect_by_base_keyword="volces",
        default_api_base="https://ark.cn-beijing.volces.com/api/v3",
    ),

    # VolcEngine Coding Plan
    ProviderSpec(
        name="volcengine_coding_plan",
        keywords=("volcengine-plan",),
        env_key="OPENAI_API_KEY",
        display_name="VolcEngine Coding Plan",
        description="火山引擎编码增强版",
        signup_url="https://console.volcengine.com/ark",
        backend="openai_compat",
        is_gateway=True,
        default_api_base="https://ark.cn-beijing.volces.com/api/coding/v3",
        strip_model_prefix=True,
    ),

    # BytePlus: VolcEngine international
    ProviderSpec(
        name="byteplus",
        keywords=("byteplus",),
        env_key="OPENAI_API_KEY",
        display_name="BytePlus",
        description="BytePlus 火山引擎国际版",
        signup_url="https://www.byteplus.com/",
        backend="openai_compat",
        is_gateway=True,
        detect_by_base_keyword="bytepluses",
        default_api_base="https://ark.ap-southeast.bytepluses.com/api/v3",
        strip_model_prefix=True,
    ),

    # BytePlus Coding Plan
    ProviderSpec(
        name="byteplus_coding_plan",
        keywords=("byteplus-plan",),
        env_key="OPENAI_API_KEY",
        display_name="BytePlus Coding Plan",
        description="BytePlus 编码增强版",
        signup_url="https://www.byteplus.com/",
        backend="openai_compat",
        is_gateway=True,
        default_api_base="https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        strip_model_prefix=True,
    ),


    # === Standard providers (matched by model-name keywords) ===============
    # Anthropic: native Anthropic SDK
    AnthropicSpec(
        name="anthropic",
        keywords=("anthropic", "claude"),
        env_key="ANTHROPIC_API_KEY",
        display_name="Anthropic",
        description="Anthropic — native Claude API",
        signup_url="https://console.anthropic.com/",
        backend="anthropic",
        supports_prompt_caching=True,
        aliases=("claude",),
        default_aux_model="claude-haiku-4-5-20251001",
    ),
    # OpenAI: SDK default base URL
    ProviderSpec(
        name="openai",
        keywords=("openai", "gpt"),
        env_key="OPENAI_API_KEY",
        display_name="OpenAI",
        description="OpenAI — GPT models",
        signup_url="https://platform.openai.com/api-keys",
        backend="openai_compat",
        aliases=("gpt",),
    ),
    # OpenAI Codex: OAuth-based, dedicated provider
    ProviderSpec(
        name="openai_codex",
        keywords=("openai-codex",),
        env_key="",
        display_name="OpenAI Codex",
        description="OpenAI Codex — OAuth-based Responses API",
        backend="openai_codex",
        detect_by_base_keyword="codex",
        default_api_base="https://chatgpt.com/backend-api",
        is_oauth=True,
        auth_type="oauth",
        aliases=("codex",),
    ),
    # GitHub Copilot: OAuth-based
    ProviderSpec(
        name="github_copilot",
        keywords=("github_copilot", "copilot"),
        env_key="",
        display_name="Github Copilot",
        description="GitHub Copilot — OAuth-based",
        signup_url="https://github.com/features/copilot",
        backend="openai_compat",
        default_api_base="https://api.githubcopilot.com",
        is_oauth=True,
        auth_type="oauth",
        aliases=("copilot", "github"),
    ),
    # DeepSeek: OpenAI-compatible with thinking/reasoning support
    DeepSeekSpec(
        name="deepseek",
        keywords=("deepseek",),
        env_key="DEEPSEEK_API_KEY",
        display_name="DeepSeek",
        description="DeepSeek — native DeepSeek API with thinking mode",
        signup_url="https://platform.deepseek.com/",
        backend="openai_compat",
        default_api_base="https://api.deepseek.com",
        aliases=("deepseek-chat",),
        fallback_models=(
            "deepseek-chat",
            "deepseek-reasoner",
        ),
        default_aux_model="deepseek-chat",
    ),
    # Gemini: Google's OpenAI-compatible endpoint with thinking_config
    GeminiSpec(
        name="gemini",
        keywords=("gemini",),
        env_key="GEMINI_API_KEY",
        display_name="Gemini",
        description="Google Gemini — OpenAI-compatible endpoint",
        signup_url="https://aistudio.google.com/apikey",
        backend="openai_compat",
        default_api_base="https://generativelanguage.googleapis.com/v1beta/openai/",
        aliases=("google", "google-gemini"),
        default_aux_model="gemini-2.5-flash-preview-05-20",
    ),
    # Zhipu (智谱): OpenAI-compatible at open.bigmodel.cn
    ProviderSpec(
        name="zhipu",
        keywords=("zhipu", "glm", "zai"),
        env_key="ZAI_API_KEY",
        display_name="Zhipu AI",
        description="Z.AI / GLM — Zhipu AI models",
        signup_url="https://z.ai/",
        backend="openai_compat",
        env_extras=(("ZHIPUAI_API_KEY", "{api_key}"),),
        default_api_base="https://open.bigmodel.cn/api/paas/v4",
        aliases=("glm", "z-ai"),
        fallback_models=("glm-4-plus", "glm-4-flash"),
        default_aux_model="glm-4-flash",
    ),
    # DashScope (通义): Qwen models with message normalization
    DashScopeSpec(
        name="dashscope",
        keywords=("qwen", "dashscope"),
        env_key="DASHSCOPE_API_KEY",
        display_name="DashScope",
        description="Alibaba Cloud DashScope — Qwen models",
        signup_url="https://dashscope.console.aliyun.com/",
        backend="openai_compat",
        default_api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        aliases=("alibaba", "alibaba-cloud", "qwen-dashscope"),
        default_aux_model="qwen-plus",
    ),
    # Moonshot (月之暗面): Kimi models with thinking support
    MoonshotSpec(
        name="moonshot",
        keywords=("moonshot", "kimi"),
        env_key="MOONSHOT_API_KEY",
        display_name="Moonshot",
        description="Moonshot / Kimi — thinking + reasoning_effort",
        signup_url="https://platform.moonshot.cn/",
        backend="openai_compat",
        default_api_base="https://api.moonshot.ai/v1",
        aliases=("kimi", "kimi-coding"),
        fixed_temperature=_OMIT_TEMPERATURE,
        default_max_tokens=32000,
        default_headers=(("User-Agent", "markbot/1.0"),),
        default_aux_model="kimi-k2-turbo-preview",
    ),
    # MiniMax: OpenAI-compatible API
    ProviderSpec(
        name="minimax",
        keywords=("minimax",),
        env_key="MINIMAX_API_KEY",
        display_name="MiniMax",
        description="MiniMax — OpenAI-compatible API",
        signup_url="https://api.minimax.io/",
        backend="openai_compat",
        default_api_base="https://api.minimax.io/v1",
        aliases=("mini-max",),
        default_aux_model="MiniMax-M2.7",
    ),
    # Mistral AI: OpenAI-compatible API
    ProviderSpec(
        name="mistral",
        keywords=("mistral",),
        env_key="MISTRAL_API_KEY",
        display_name="Mistral",
        description="Mistral AI — OpenAI-compatible API",
        signup_url="https://console.mistral.ai/",
        backend="openai_compat",
        default_api_base="https://api.mistral.ai/v1",
    ),
    # Step Fun (阶跃星辰): OpenAI-compatible API
    ProviderSpec(
        name="stepfun",
        keywords=("stepfun", "step"),
        env_key="STEPFUN_API_KEY",
        display_name="Step Fun",
        description="Step Fun — 阶跃星辰",
        signup_url="https://platform.stepfun.com/",
        backend="openai_compat",
        default_api_base="https://api.stepfun.com/v1",
        default_aux_model="step-2-flash",
    ),
    # xAI (Grok): OpenAI-compatible API
    ProviderSpec(
        name="xai",
        keywords=("xai", "grok"),
        env_key="XAI_API_KEY",
        display_name="xAI",
        description="xAI — Grok models",
        signup_url="https://console.x.ai/",
        backend="openai_compat",
        default_api_base="https://api.x.ai/v1",
        aliases=("grok", "x-ai"),
    ),
    # NVIDIA NIM: accelerated inference
    ProviderSpec(
        name="nvidia",
        keywords=("nvidia", "nim"),
        env_key="NVIDIA_API_KEY",
        display_name="NVIDIA NIM",
        description="NVIDIA NIM — accelerated inference",
        signup_url="https://build.nvidia.com/",
        backend="openai_compat",
        default_api_base="https://integrate.api.nvidia.com/v1",
        aliases=("nvidia-nim",),
        fallback_models=(
            "nvidia/llama-3.1-nemotron-70b-instruct",
        ),
        default_max_tokens=16384,
    ),
    # HuggingFace Inference API
    ProviderSpec(
        name="huggingface",
        keywords=("huggingface", "hf"),
        env_key="HF_TOKEN",
        display_name="HuggingFace",
        description="HuggingFace Inference API",
        signup_url="https://huggingface.co/settings/tokens",
        backend="openai_compat",
        default_api_base="https://router.huggingface.co/v1",
        aliases=("hf", "hugging-face"),
    ),
    # === Local deployment (matched by config key, NOT by api_base) =========
    # vLLM / any OpenAI-compatible local server
    ProviderSpec(
        name="vllm",
        keywords=("vllm",),
        env_key="HOSTED_VLLM_API_KEY",
        display_name="vLLM/Local",
        backend="openai_compat",
        is_local=True,
    ),
    # Ollama (local, OpenAI-compatible)
    ProviderSpec(
        name="ollama",
        keywords=("ollama-local",),
        env_key="OLLAMA_API_KEY",
        display_name="Ollama",
        description="Ollama — local LLM deployment",
        backend="openai_compat",
        is_local=True,
        detect_by_base_keyword="11434",
        default_api_base="http://localhost:11434/v1",
    ),
    # === OpenVINO Model Server (direct, local, OpenAI-compatible at /v3) ===
    ProviderSpec(
        name="ovms",
        keywords=("openvino", "ovms"),
        env_key="",
        display_name="OpenVINO Model Server",
        backend="openai_compat",
        is_direct=True,
        is_local=True,
        default_api_base="http://localhost:8000/v3",
    ),
    # === Auxiliary (not a primary LLM provider) ============================
    # Groq: mainly used for Whisper voice transcription, also usable for LLM
    ProviderSpec(
        name="groq",
        keywords=("groq",),
        env_key="GROQ_API_KEY",
        display_name="Groq",
        description="Groq — fast inference + Whisper transcription",
        signup_url="https://console.groq.com/",
        backend="openai_compat",
        default_api_base="https://api.groq.com/openai/v1",
    ),
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def find_by_name(name: str) -> ProviderSpec | None:
    """Find a provider spec by config field name or alias.

    Checks name first, then aliases. Name matching normalizes hyphens
    to underscores and applies snake_case conversion.
    """
    normalized = to_snake(name.replace("-", "_"))
    for spec in PROVIDERS:
        if spec.name == normalized:
            return spec
    name_lower = name.lower()
    for spec in PROVIDERS:
        if name_lower in spec.aliases:
            return spec
    return None


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from markbot.providers.base import LLMProvider

_PROVIDER_FACTORIES: dict[str, type["LLMProvider"]] = {}
_FACTORIES_LOADED = False


def register_provider_factory(backend: str, cls: type["LLMProvider"]) -> None:
    """Register a provider class for a backend name.

    Called from each provider module at import time so that
    ``create_provider`` can instantiate the right class without
    if-elif chains.
    """
    _PROVIDER_FACTORIES[backend] = cls


def _ensure_factories_loaded() -> None:
    """Lazy-load provider modules so their ``register_provider_factory`` calls execute.

    Defers imports until first call to :func:`create_provider` to avoid
    circular-import issues at module level.
    """
    global _FACTORIES_LOADED
    if _FACTORIES_LOADED:
        return
    _FACTORIES_LOADED = True
    for backend, module_path in (
        ("anthropic", "markbot.providers.anthropic"),
        ("azure_openai", "markbot.providers.azure_openai"),
        ("openai_compat", "markbot.providers.openai_compat"),
        ("openai_codex", "markbot.providers.openai_codex"),
    ):
        if backend not in _PROVIDER_FACTORIES:
            try:
                __import__(module_path)
            except ImportError:
                pass


def create_provider(
    backend: str,
    api_key: str | None = None,
    api_base: str | None = None,
    extra_headers: dict[str, str] | None = None,
    spec: "ProviderSpec | None" = None,
) -> "LLMProvider":
    """Instantiate a provider by backend name using the factory registry.

    Falls back to OpenAICompatProvider for unknown backends.
    """
    _ensure_factories_loaded()

    cls = _PROVIDER_FACTORIES.get(backend)
    if cls is not None:
        return cls(api_key=api_key, api_base=api_base, extra_headers=extra_headers, spec=spec)

    from markbot.providers.openai_compat import OpenAICompatProvider

    logger.warning("Unknown backend '{}', falling back to OpenAICompatProvider", backend)
    return OpenAICompatProvider(api_key=api_key, api_base=api_base, extra_headers=extra_headers, spec=spec)
