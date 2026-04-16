"""Multi-model fallback chain management."""

from dataclasses import dataclass
from typing import Any

from loguru import logger

from markbot.config.schema import Config, ModelConfig, ProviderConfig
from markbot.providers.base import LLMProvider, LLMResponse


@dataclass
class FallbackAttempt:
    """Record of a single fallback attempt."""
    model_ref: str
    provider: ProviderConfig | None = None
    model: ModelConfig | None = None
    success: bool = False
    error: str | None = None
    response: LLMResponse | None = None


class AllModelsFailedError(Exception):
    """Raised when all models in the chain have failed."""

    def __init__(self, message: str, attempts: list[FallbackAttempt], last_error: Exception | None):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


class FallbackManager:
    """
    Manages multi-model fallback chain.

    Usage:
        manager = FallbackManager(config)
        response = await manager.chat_with_fallback(messages, tools)
    """

    RETRYABLE_ERRORS = (
        "429", "529", "rate limit", "500", "502", "503", "504",
        "overloaded", "timeout", "timed out", "connection",
        "server error", "temporarily unavailable"
    )

    def __init__(self, config: Config):
        self.config = config
        self._providers_cache: dict[str, LLMProvider] = {}

    def _is_retryable_error(self, error: Exception) -> bool:
        """Check if error is retryable (should trigger fallback to next model)."""
        err_str = str(error).lower()
        return any(marker in err_str for marker in self.RETRYABLE_ERRORS)

    def _get_or_create_provider(self, provider_config: ProviderConfig, provider_name: str) -> LLMProvider:
        """Create or cache LLM provider instance."""
        cache_key = provider_name

        if cache_key not in self._providers_cache:
            from markbot.providers.registry import find_by_name
            spec = find_by_name(provider_name)
            backend = spec.backend if spec else "openai_compat"

            if backend == "anthropic":
                from markbot.providers.anthropic_provider import AnthropicProvider
                self._providers_cache[cache_key] = AnthropicProvider(
                    api_key=provider_config.api_key,
                    api_base=provider_config.api_base,
                )
            elif backend == "azure_openai":
                from markbot.providers.azure_openai_provider import AzureOpenAIProvider
                self._providers_cache[cache_key] = AzureOpenAIProvider(
                    api_key=provider_config.api_key,
                    api_base=provider_config.api_base,
                )
            else:
                from markbot.providers.openai_compat_provider import OpenAICompatProvider
                self._providers_cache[cache_key] = OpenAICompatProvider(
                    api_key=provider_config.api_key,
                    api_base=provider_config.api_base,
                )

        return self._providers_cache[cache_key]

    async def chat_with_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> tuple[LLMResponse, list[FallbackAttempt]]:
        """
        Try each model in chain until success.

        Returns:
            Tuple of (response, attempt_history)
        """
        attempts = []
        last_error = None

        for model_ref in self.config.agents.defaults.model_chain:
            try:
                provider_config, model_config = self.config.resolve_model(model_ref)
                provider_name = model_ref.split("/")[0]

                provider = self._get_or_create_provider(provider_config, provider_name)

                logger.info(f"Trying model: {model_ref}")
                response = await provider.chat(
                    messages=messages,
                    tools=tools,
                    model=model_config.name,
                    max_tokens=model_config.max_tokens or self.config.agents.defaults.max_tokens,
                    temperature=model_config.temperature or self.config.agents.defaults.temperature,
                    reasoning_effort=model_config.reasoning_effort or self.config.agents.defaults.reasoning_effort,
                    tool_choice=tool_choice,
                )

                attempt = FallbackAttempt(
                    model_ref=model_ref,
                    provider=provider_config,
                    model=model_config,
                    success=True,
                    response=response,
                )
                attempts.append(attempt)

                logger.info(f"Model {model_ref} succeeded")
                return response, attempts

            except Exception as e:
                attempt = FallbackAttempt(
                    model_ref=model_ref,
                    provider=provider_config if 'provider_config' in locals() else None,
                    model=model_config if 'model_config' in locals() else None,
                    success=False,
                    error=str(e),
                )
                attempts.append(attempt)

                if self._is_retryable_error(e):
                    logger.warning(f"Model {model_ref} failed (retryable): {e}. Trying next...")
                    last_error = e
                    continue
                else:
                    logger.error(f"Model {model_ref} failed (non-retryable): {e}")
                    raise

        raise AllModelsFailedError(
            f"All {len(attempts)} models in chain failed",
            attempts=attempts,
            last_error=last_error,
        )

    async def chat_stream_with_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Any | None = None,
    ) -> tuple[LLMResponse, list[FallbackAttempt]]:
        """Try each model in chain with streaming until success.

        Like chat_with_fallback but uses provider.chat_stream when available,
        calling *on_content_delta* for each text chunk.

        Returns:
            Tuple of (response, attempt_history)
        """
        from typing import Callable, Awaitable

        attempts: list[FallbackAttempt] = []
        last_error: Exception | None = None

        for model_ref in self.config.agents.defaults.model_chain:
            try:
                provider_config, model_config = self.config.resolve_model(model_ref)
                provider_name = model_ref.split("/")[0]

                provider = self._get_or_create_provider(provider_config, provider_name)

                logger.info(f"Trying model (stream): {model_ref}")
                response = await provider.chat_stream(
                    messages=messages,
                    tools=tools,
                    model=model_config.name,
                    max_tokens=model_config.max_tokens or self.config.agents.defaults.max_tokens,
                    temperature=model_config.temperature or self.config.agents.defaults.temperature,
                    reasoning_effort=model_config.reasoning_effort or self.config.agents.defaults.reasoning_effort,
                    tool_choice=tool_choice,
                    on_content_delta=on_content_delta,
                )

                attempt = FallbackAttempt(
                    model_ref=model_ref,
                    provider=provider_config,
                    model=model_config,
                    success=True,
                    response=response,
                )
                attempts.append(attempt)

                logger.info(f"Model {model_ref} succeeded (stream)")
                return response, attempts

            except Exception as e:
                attempt = FallbackAttempt(
                    model_ref=model_ref,
                    provider=provider_config if 'provider_config' in locals() else None,
                    model=model_config if 'model_config' in locals() else None,
                    success=False,
                    error=str(e),
                )
                attempts.append(attempt)

                if self._is_retryable_error(e):
                    logger.warning(f"Model {model_ref} failed (retryable): {e}. Trying next...")
                    last_error = e
                    continue
                else:
                    logger.error(f"Model {model_ref} failed (non-retryable): {e}")
                    raise

        raise AllModelsFailedError(
            f"All {len(attempts)} models in chain failed",
            attempts=attempts,
            last_error=last_error,
        )
