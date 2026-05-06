"""Multi-model fallback chain management."""

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from markbot.config.schema import Config, ModelConfig, ProviderConfig
from markbot.providers.base import LLMProvider, LLMResponse


@dataclass
class CircuitState:
    """Per-provider circuit breaker state."""

    failure_count: int = 0
    last_failure_time: float = 0.0
    state: str = "closed"

    @property
    def is_open(self) -> bool:
        return self.state == "open"


@dataclass
class FallbackAttempt:
    """Record of a single fallback attempt."""

    model_ref: str
    provider: ProviderConfig | None = None
    model: ModelConfig | None = None
    success: bool = False
    error: str | None = None
    response: LLMResponse | None = None
    circuit_skipped: bool = False


class AllModelsFailedError(Exception):
    """Raised when all models in the chain have failed."""

    def __init__(self, message: str, attempts: list[FallbackAttempt], last_error: Exception | None):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


class FallbackManager:
    """Manages multi-model fallback chain with circuit breaker.

    Usage:
        manager = FallbackManager(config)
        response = await manager.chat_with_fallback(messages, tools)
    """

    MODEL_UNAVAILABLE_ERRORS = (
        "402", "insufficient balance", "insufficient_quota", "quota exceeded",
        "401", "unauthorized", "invalid api key", "authentication",
        "403", "forbidden", "access denied",
        "model not found", "model_not_found",
        "invalid function arguments", "invalid params",
    )

    _TRANSIENT_ERROR_MARKERS = (
        "timeout", "timed out", "connection", "rate limit", "rate_limit",
        "too many requests", "server error", "internal server error",
        "service unavailable", "overloaded", "capacity", "busy",
        "try again", "retry", "503", "502", "504", "529",
        "temporarily unavailable", "throttle",
    )

    DEFAULT_CIRCUIT_THRESHOLD = 3
    DEFAULT_CIRCUIT_COOLDOWN = 60.0

    def __init__(self, config: Config):
        self.config = config
        self._providers_cache: dict[str, LLMProvider] = {}
        self._circuits: dict[str, CircuitState] = {}
        self._circuit_threshold = self.DEFAULT_CIRCUIT_THRESHOLD
        self._circuit_cooldown = self.DEFAULT_CIRCUIT_COOLDOWN

    @staticmethod
    def _is_retryable_error(error: Exception | str) -> bool:
        err_str = str(error).lower()
        return any(marker in err_str for marker in FallbackManager._TRANSIENT_ERROR_MARKERS)

    def _get_circuit(self, provider_name: str) -> CircuitState:
        if provider_name not in self._circuits:
            self._circuits[provider_name] = CircuitState()
        return self._circuits[provider_name]

    def _check_circuit(self, provider_name: str) -> bool:
        circuit = self._get_circuit(provider_name)
        if circuit.state == "closed":
            return True
        if circuit.state == "open":
            elapsed = time.monotonic() - circuit.last_failure_time
            if elapsed >= self._circuit_cooldown:
                circuit.state = "half-open"
                logger.info(f"[CircuitBreaker] {provider_name} half-open (cooldown elapsed)")
                return True
            logger.warning(
                f"[CircuitBreaker] {provider_name} circuit open, skipping "
                f"(failures={circuit.failure_count}, retry in {self._circuit_cooldown - elapsed:.0f}s)"
            )
            return False
        return True

    def _record_success(self, provider_name: str) -> None:
        circuit = self._get_circuit(provider_name)
        if circuit.state != "closed":
            logger.info(f"[CircuitBreaker] {provider_name} circuit closed (recovered)")
        circuit.failure_count = 0
        circuit.state = "closed"

    def _record_failure(self, provider_name: str) -> None:
        circuit = self._get_circuit(provider_name)
        circuit.failure_count += 1
        circuit.last_failure_time = time.monotonic()
        if circuit.failure_count >= self._circuit_threshold:
            circuit.state = "open"
            logger.warning(
                f"[CircuitBreaker] {provider_name} circuit OPEN "
                f"({circuit.failure_count} consecutive failures)"
            )

    def _is_model_unavailable_error(self, error: Exception | str) -> bool:
        err_str = str(error).lower()
        return any(marker in err_str for marker in self.MODEL_UNAVAILABLE_ERRORS)

    def _get_or_create_provider(self, provider_config: ProviderConfig, provider_name: str) -> LLMProvider:
        cache_key = provider_name
        if cache_key not in self._providers_cache:
            from markbot.providers.registry import find_by_name

            spec = find_by_name(provider_name)
            backend = spec.backend if spec else "openai_compat"

            if backend == "anthropic":
                from markbot.providers.anthropic import AnthropicProvider
                self._providers_cache[cache_key] = AnthropicProvider(
                    api_key=provider_config.api_key,
                    api_base=provider_config.api_base,
                )
            elif backend == "azure_openai":
                from markbot.providers.azure_openai import AzureOpenAIProvider
                self._providers_cache[cache_key] = AzureOpenAIProvider(
                    api_key=provider_config.api_key,
                    api_base=provider_config.api_base,
                )
            else:
                from markbot.providers.openai_compat import OpenAICompatProvider
                self._providers_cache[cache_key] = OpenAICompatProvider(
                    api_key=provider_config.api_key,
                    api_base=provider_config.api_base,
                )

        return self._providers_cache[cache_key]

    # Callable that invokes a specific LLM provider method (chat or chat_stream).
    _ModelCaller = Callable[
        [LLMProvider, ModelConfig, int, float, str | None],
        Awaitable[LLMResponse],
    ]

    async def _try_chain(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        max_tokens: int | None,
        temperature: float | None,
        caller: "_ModelCaller",
    ) -> tuple[LLMResponse, list[FallbackAttempt]]:
        """Try each model in chain using *caller* for the actual LLM invocation.

        All error classification, attempt recording, circuit breaker,
        and retry logic is handled here so that ``chat_with_fallback``
        and ``chat_stream_with_fallback`` only differ in the provider
        method they pass as the *caller*.
        """
        attempts: list[FallbackAttempt] = []
        last_error: Exception | None = None
        defaults = self.config.agents.defaults

        for model_ref in defaults.model_chain:
            provider_config: ProviderConfig | None = None
            model_config: ModelConfig | None = None
            provider_name = model_ref.split("/")[0]

            if not self._check_circuit(provider_name):
                attempt = FallbackAttempt(
                    model_ref=model_ref,
                    provider=provider_config,
                    model=model_config,
                    success=False,
                    error="Circuit breaker open",
                    circuit_skipped=True,
                )
                attempts.append(attempt)
                continue

            try:
                provider_config, model_config = self.config.resolve_model(model_ref)
                provider = self._get_or_create_provider(provider_config, provider_name)

                _max_tokens = max_tokens or model_config.max_tokens or defaults.max_tokens
                _temperature = (
                    temperature
                    if temperature is not None
                    else (model_config.temperature or defaults.temperature)
                )
                _reasoning = model_config.reasoning_effort or defaults.reasoning_effort

                logger.info(f"Trying model: {model_ref}")
                response = await caller(provider, model_config, _max_tokens, _temperature, _reasoning)

                if response.finish_reason == "error":
                    error_msg = response.content or "Unknown error"
                    attempt = FallbackAttempt(
                        model_ref=model_ref,
                        provider=provider_config,
                        model=model_config,
                        success=False,
                        error=error_msg,
                    )
                    attempts.append(attempt)

                    if self._is_retryable_error(error_msg):
                        logger.warning(
                            f"Model {model_ref} returned error (retryable): {error_msg}. Trying next..."
                        )
                        last_error = Exception(error_msg)
                        self._record_failure(provider_name)
                        continue
                    elif self._is_model_unavailable_error(error_msg):
                        logger.warning(
                            f"Model {model_ref} unavailable: {error_msg}. Trying next..."
                        )
                        last_error = Exception(error_msg)
                        self._record_failure(provider_name)
                        continue
                    else:
                        logger.error(
                            f"Model {model_ref} returned error (non-retryable): {error_msg}"
                        )
                        self._record_failure(provider_name)
                        raise AllModelsFailedError(
                            f"Model {model_ref} failed with non-retryable error",
                            attempts=attempts,
                            last_error=Exception(error_msg),
                        )

                self._record_success(provider_name)
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

            except AllModelsFailedError:
                raise
            except Exception as e:
                attempt = FallbackAttempt(
                    model_ref=model_ref,
                    provider=provider_config,
                    model=model_config,
                    success=False,
                    error=str(e),
                )
                attempts.append(attempt)

                if self._is_retryable_error(e):
                    logger.warning(
                        f"Model {model_ref} failed (retryable): {e}. Trying next..."
                    )
                    last_error = e
                    self._record_failure(provider_name)
                    continue
                elif self._is_model_unavailable_error(e):
                    logger.warning(
                        f"Model {model_ref} unavailable: {e}. Trying next..."
                    )
                    last_error = e
                    self._record_failure(provider_name)
                    continue
                else:
                    logger.error(f"Model {model_ref} failed (non-retryable): {e}")
                    self._record_failure(provider_name)
                    raise

        raise AllModelsFailedError(
            f"All {len(attempts)} models in chain failed",
            attempts=attempts,
            last_error=last_error,
        )

    async def chat_with_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> tuple[LLMResponse, list[FallbackAttempt]]:
        """Try each model in chain until success."""

        async def _call_chat(
            provider: LLMProvider,
            model_config: ModelConfig,
            max_tok: int,
            temp: float,
            reasoning: str | None,
        ) -> LLMResponse:
            return await provider.chat(
                messages=messages,
                tools=tools,
                model=model_config.name,
                max_tokens=max_tok,
                temperature=temp,
                reasoning_effort=reasoning,
                tool_choice=tool_choice,
            )

        return await self._try_chain(messages, tools, tool_choice, max_tokens, temperature, _call_chat)

    async def chat_stream_with_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Any | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> tuple[LLMResponse, list[FallbackAttempt]]:
        """Try each model in chain with streaming until success."""

        async def _call_stream(
            provider: LLMProvider,
            model_config: ModelConfig,
            max_tok: int,
            temp: float,
            reasoning: str | None,
        ) -> LLMResponse:
            return await provider.chat_stream(
                messages=messages,
                tools=tools,
                model=model_config.name,
                max_tokens=max_tok,
                temperature=temp,
                reasoning_effort=reasoning,
                tool_choice=tool_choice,
                on_content_delta=on_content_delta,
            )

        return await self._try_chain(messages, tools, tool_choice, max_tokens, temperature, _call_stream)
