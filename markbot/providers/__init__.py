"""LLM provider abstraction module."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from markbot.providers.base import LLMProvider, LLMResponse
from markbot.providers.registry import (
    ProviderSpec,
    DeepSeekSpec,
    MoonshotSpec,
    OpenRouterSpec,
    GeminiSpec,
    CustomSpec,
    DashScopeSpec,
    AnthropicSpec,
    PROVIDERS,
    _OMIT_TEMPERATURE,
    find_by_name,
    create_provider,
)

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "AnthropicProvider",
    "OpenAICompatProvider",
    "OpenAICodexProvider",
    "AzureOpenAIProvider",
    "FallbackManager",
    "AllModelsFailedError",
    "FallbackAttempt",
    "ProviderSpec",
    "DeepSeekSpec",
    "MoonshotSpec",
    "OpenRouterSpec",
    "GeminiSpec",
    "CustomSpec",
    "DashScopeSpec",
    "AnthropicSpec",
    "PROVIDERS",
    "_OMIT_TEMPERATURE",
    "find_by_name",
    "create_provider",
]

_LAZY_IMPORTS = {
    "AnthropicProvider": ".anthropic",
    "OpenAICompatProvider": ".openai_compat",
    "OpenAICodexProvider": ".openai_codex",
    "AzureOpenAIProvider": ".azure_openai",
    "FallbackManager": ".fallback",
    "AllModelsFailedError": ".fallback",
    "FallbackAttempt": ".fallback",
}

if TYPE_CHECKING:
    from markbot.providers.anthropic import AnthropicProvider
    from markbot.providers.azure_openai import AzureOpenAIProvider
    from markbot.providers.openai_compat import OpenAICompatProvider
    from markbot.providers.openai_codex import OpenAICodexProvider
    from markbot.providers.fallback import FallbackManager, AllModelsFailedError, FallbackAttempt


def __getattr__(name: str):
    """Lazily expose provider implementations without importing all backends up front."""
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name, __name__)
    return getattr(module, name)
