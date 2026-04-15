"""Model database for onboard wizard (V2).

Provides comprehensive model information organized by provider,
including context window sizes, capabilities, and pricing hints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelInfo:
    """Information about a specific model."""
    id: str
    name: str
    display_name: str
    max_tokens: int = 8192
    context_window: int = 65536
    supports_reasoning: bool = False
    supports_vision: bool = False
    supports_tools: bool = True
    description: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class ProviderModels:
    """Models available for a provider."""
    provider_id: str
    display_name: str
    description: str = ""
    requires_api_key: bool = True
    requires_api_base: bool = False
    default_api_base: str | None = None
    models: list[ModelInfo] = field(default_factory=list)

    def get_model(self, model_id: str) -> ModelInfo | None:
        """Get model info by ID."""
        return next((m for m in self.models if m.id == model_id), None)


# Model database
MODEL_DATABASE: dict[str, ProviderModels] = {
    "anthropic": ProviderModels(
        provider_id="anthropic",
        display_name="Anthropic",
        description="Claude series models with strong reasoning and coding abilities",
        default_api_base="https://api.anthropic.com",
        models=[
            ModelInfo(
                id="claude-opus",
                name="claude-opus-4-5",
                display_name="Claude Opus 4.5",
                max_tokens=8192,
                context_window=200000,
                supports_reasoning=True,
                supports_vision=True,
                description="Most capable Claude model for complex tasks",
                tags=["flagship", "reasoning", "vision"],
            ),
            ModelInfo(
                id="claude-sonnet",
                name="claude-sonnet-4-5",
                display_name="Claude Sonnet 4.5",
                max_tokens=8192,
                context_window=200000,
                supports_reasoning=True,
                supports_vision=True,
                description="Balanced performance and speed",
                tags=["balanced", "reasoning", "vision"],
            ),
            ModelInfo(
                id="claude-haiku",
                name="claude-haiku-4-5",
                display_name="Claude Haiku 4.5",
                max_tokens=8192,
                context_window=200000,
                supports_reasoning=False,
                supports_vision=True,
                description="Fast and cost-effective for simple tasks",
                tags=["fast", "cost-effective", "vision"],
            ),
        ],
    ),
    "openai": ProviderModels(
        provider_id="openai",
        display_name="OpenAI",
        description="GPT series models with broad capabilities",
        default_api_base="https://api.openai.com/v1",
        models=[
            ModelInfo(
                id="gpt-4o",
                name="gpt-4o",
                display_name="GPT-4o",
                max_tokens=16384,
                context_window=128000,
                supports_vision=True,
                description="Multimodal flagship model",
                tags=["flagship", "vision", "multimodal"],
            ),
            ModelInfo(
                id="gpt-4o-mini",
                name="gpt-4o-mini",
                display_name="GPT-4o Mini",
                max_tokens=16384,
                context_window=128000,
                supports_vision=True,
                description="Cost-effective multimodal model",
                tags=["cost-effective", "vision"],
            ),
            ModelInfo(
                id="gpt-4-turbo",
                name="gpt-4-turbo",
                display_name="GPT-4 Turbo",
                max_tokens=4096,
                context_window=128000,
                description="High-performance GPT-4 variant",
                tags=["performance"],
            ),
            ModelInfo(
                id="o3",
                name="o3",
                display_name="O3",
                max_tokens=100000,
                context_window=200000,
                supports_reasoning=True,
                description="Advanced reasoning model",
                tags=["reasoning", "flagship"],
            ),
            ModelInfo(
                id="o3-mini",
                name="o3-mini",
                display_name="O3 Mini",
                max_tokens=100000,
                context_window=200000,
                supports_reasoning=True,
                description="Cost-effective reasoning model",
                tags=["reasoning", "cost-effective"],
            ),
        ],
    ),
    "deepseek": ProviderModels(
        provider_id="deepseek",
        display_name="DeepSeek",
        description="Strong coding and reasoning models from DeepSeek",
        default_api_base="https://api.deepseek.com",
        models=[
            ModelInfo(
                id="deepseek-chat",
                name="deepseek-chat",
                display_name="DeepSeek Chat",
                max_tokens=8192,
                context_window=65536,
                description="General-purpose chat model",
                tags=["general", "coding"],
            ),
            ModelInfo(
                id="deepseek-reasoner",
                name="deepseek-reasoner",
                display_name="DeepSeek Reasoner (R1)",
                max_tokens=8192,
                context_window=65536,
                supports_reasoning=True,
                description="Advanced reasoning model (R1)",
                tags=["reasoning", "coding", "flagship"],
            ),
        ],
    ),
    "openrouter": ProviderModels(
        provider_id="openrouter",
        display_name="OpenRouter",
        description="Unified API gateway for multiple providers",
        default_api_base="https://openrouter.ai/api/v1",
        models=[
            ModelInfo(
                id="anthropic/claude-3.5-sonnet",
                name="anthropic/claude-3.5-sonnet",
                display_name="Claude 3.5 Sonnet (via OpenRouter)",
                max_tokens=8192,
                context_window=200000,
                description="Access Claude via OpenRouter aggregation",
                tags=["aggregated", "balanced"],
            ),
            ModelInfo(
                id="openai/gpt-4o",
                name="openai/gpt-4o",
                display_name="GPT-4o (via OpenRouter)",
                max_tokens=16384,
                context_window=128000,
                supports_vision=True,
                description="Access GPT-4o via OpenRouter",
                tags=["aggregated", "vision"],
            ),
            ModelInfo(
                id="deepseek/deepseek-chat",
                name="deepseek/deepseek-chat",
                display_name="DeepSeek Chat (via OpenRouter)",
                max_tokens=8192,
                context_window=65536,
                description="Access DeepSeek via OpenRouter",
                tags=["aggregated", "cost-effective"],
            ),
        ],
    ),
    "groq": ProviderModels(
        provider_id="groq",
        display_name="Groq",
        description="Ultra-fast inference with LPU acceleration",
        default_api_base="https://api.groq.com/openai/v1",
        models=[
            ModelInfo(
                id="llama-3.3-70b-versatile",
                name="llama-3.3-70b-versatile",
                display_name="Llama 3.3 70B Versatile",
                max_tokens=8192,
                context_window=131072,
                description="Fast open-source model on Groq LPU",
                tags=["fast", "open-source"],
            ),
            ModelInfo(
                id="llama-3.1-8b-instant",
                name="llama-3.1-8b-instant",
                display_name="Llama 3.1 8B Instant",
                max_tokens=8192,
                context_window=131072,
                description="Ultra-fast lightweight model",
                tags=["fast", "lightweight", "cost-effective"],
            ),
            ModelInfo(
                id="mixtral-8x7b-32768",
                name="mixtral-8x7b-32768",
                display_name="Mixtral 8x7B",
                max_tokens=8192,
                context_window=32768,
                description="MoE architecture for diverse tasks",
                tags=["moe", "fast"],
            ),
        ],
    ),
    "ollama": ProviderModels(
        provider_id="ollama",
        display_name="Ollama (Local)",
        description="Run models locally with Ollama",
        requires_api_key=False,
        requires_api_base=True,
        default_api_base="http://localhost:11434",
        models=[
            ModelInfo(
                id="llama3.2",
                name="llama3.2",
                display_name="Llama 3.2",
                max_tokens=8192,
                context_window=131072,
                description="Meta's latest open-source model",
                tags=["local", "open-source", "privacy"],
            ),
            ModelInfo(
                id="qwen2.5",
                name="qwen2.5",
                display_name="Qwen 2.5",
                max_tokens=8192,
                context_window=131072,
                description="Alibaba's multilingual model",
                tags=["local", "multilingual", "open-source"],
            ),
            ModelInfo(
                id="codestral",
                name="codestral",
                display_name="Codestral",
                max_tokens=8192,
                context_window=32768,
                description="Mistral's code-focused model",
                tags=["local", "coding", "open-source"],
            ),
            ModelInfo(
                id="deepseek-r1",
                name="deepseek-r1",
                display_name="DeepSeek R1",
                max_tokens=8192,
                context_window=65536,
                supports_reasoning=True,
                description="Local reasoning model",
                tags=["local", "reasoning", "open-source"],
            ),
        ],
    ),
    "zhipu": ProviderModels(
        provider_id="zhipu",
        display_name="Zhipu AI (智谱)",
        description="Chinese AI models with strong Chinese language support",
        default_api_base="https://open.bigmodel.cn/api/paas/v4",
        models=[
            ModelInfo(
                id="glm-4-plus",
                name="glm-4-plus",
                display_name="GLM-4 Plus",
                max_tokens=4096,
                context_window=128000,
                description="Flagship model with enhanced capabilities",
                tags=["flagship", "chinese"],
            ),
            ModelInfo(
                id="glm-4-air",
                name="glm-4-air",
                display_name="GLM-4 Air",
                max_tokens=4096,
                context_window=128000,
                description="Cost-effective general model",
                tags=["cost-effective", "chinese"],
            ),
            ModelInfo(
                id="glm-4-flash",
                name="glm-4-flash",
                display_name="GLM-4 Flash",
                max_tokens=4096,
                context_window=128000,
                description="Fast free-tier model",
                tags=["fast", "free", "chinese"],
            ),
        ],
    ),
    "gemini": ProviderModels(
        provider_id="gemini",
        display_name="Google Gemini",
        description="Google's multimodal AI models",
        default_api_base="https://generativelanguage.googleapis.com/v1beta",
        models=[
            ModelInfo(
                id="gemini-2.5-pro",
                name="gemini-2.5-pro",
                display_name="Gemini 2.5 Pro",
                max_tokens=8192,
                context_window=1048576,
                supports_reasoning=True,
                supports_vision=True,
                description="Google's most capable model with 1M context",
                tags=["flagship", "reasoning", "vision", "large-context"],
            ),
            ModelInfo(
                id="gemini-2.5-flash",
                name="gemini-2.5-flash",
                display_name="Gemini 2.5 Flash",
                max_tokens=8192,
                context_window=1048576,
                supports_vision=True,
                description="Fast model with large context window",
                tags=["fast", "vision", "large-context"],
            ),
        ],
    ),
}


def get_all_providers() -> list[ProviderModels]:
    """Get all available providers."""
    return list(MODEL_DATABASE.values())


def get_provider(provider_id: str) -> ProviderModels | None:
    """Get provider by ID."""
    return MODEL_DATABASE.get(provider_id)


def get_provider_models(provider_id: str) -> list[ModelInfo]:
    """Get all models for a provider."""
    provider = MODEL_DATABASE.get(provider_id)
    return provider.models if provider else []


def get_model_info(provider_id: str, model_id: str) -> ModelInfo | None:
    """Get detailed info about a specific model."""
    provider = MODEL_DATABASE.get(provider_id)
    if not provider:
        return None
    return provider.get_model(model_id)


def get_all_models() -> list[dict[str, Any]]:
    """Get all models as flat list with provider info."""
    result = []
    for provider in MODEL_DATABASE.values():
        for model in provider.models:
            result.append({
                "provider_id": provider.provider_id,
                "provider_display": provider.display_name,
                "model_id": model.id,
                "model_name": model.name,
                "display_name": f"{provider.display_name} / {model.display_name}",
                "max_tokens": model.max_tokens,
                "context_window": model.context_window,
                "supports_reasoning": model.supports_reasoning,
                "supports_vision": model.supports_vision,
                "description": model.description,
                "tags": model.tags,
                "ref": f"{provider.provider_id}/{model.model_id}",
            })
    return result


def find_model_info(ref: str) -> dict[str, Any] | None:
    """Find model info by reference (providerId/modelId)."""
    if "/" not in ref:
        return None

    provider_id, model_id = ref.split("/", 1)
    model = get_model_info(provider_id, model_id)
    if not model:
        return None

    provider = MODEL_DATABASE[provider_id]
    return {
        "provider_id": provider.provider_id,
        "provider_display": provider.display_name,
        "model_id": model.id,
        "model_name": model.name,
        "display_name": f"{provider.display_name} / {model.display_name}",
        "max_tokens": model.max_tokens,
        "context_window": model.context_window,
        "supports_reasoning": model.supports_reasoning,
        "supports_vision": model.supports_vision,
        "description": model.description,
        "tags": model.tags,
        "ref": ref,
    }


def get_model_context_limit(ref: str) -> int | None:
    """Get context window size for a model by reference."""
    info = find_model_info(ref)
    return info["context_window"] if info else None


def format_token_count(tokens: int) -> str:
    """Format token count for display (e.g., 200000 -> '200,000')."""
    return f"{tokens:,}"
