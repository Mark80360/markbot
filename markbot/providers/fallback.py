"""Multi-model fallback chain management."""

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from markbot.config.schema import Config, ModelConfig, ProviderConfig
from markbot.providers.base import LLMProvider, LLMResponse
from markbot.providers.errors import ErrorType, classify_error


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

    DEFAULT_CIRCUIT_THRESHOLD = 6
    DEFAULT_CIRCUIT_COOLDOWN = 60.0

    def __init__(self, config: Config):
        self.config = config
        self._providers_cache: dict[str, LLMProvider] = {}
        self._circuits: dict[str, CircuitState] = {}
        self._circuit_threshold = self.DEFAULT_CIRCUIT_THRESHOLD
        self._circuit_cooldown = self.DEFAULT_CIRCUIT_COOLDOWN
        self._half_open_probes: set[str] = set()

    # The circuit breaker is keyed by the full ``model_ref`` (e.g.
    # ``custom/deepseek-v4-flash``), NOT by provider name. Keying by
    # provider name would make every model on the same provider share
    # one circuit — a run of failures on one model would take down
    # every other model on that provider, defeating the redundancy
    # the fallback chain is supposed to provide.

    def _get_circuit(self, circuit_key: str) -> CircuitState:
        if circuit_key not in self._circuits:
            self._circuits[circuit_key] = CircuitState()
        return self._circuits[circuit_key]

    def _check_circuit(self, circuit_key: str) -> bool:
        circuit = self._get_circuit(circuit_key)
        if circuit.state == "closed":
            return True
        if circuit.state == "open":
            elapsed = time.monotonic() - circuit.last_failure_time
            if elapsed >= self._circuit_cooldown:
                circuit.state = "half-open"
                self._half_open_probes.discard(circuit_key)
                logger.info("{} half-open (cooldown elapsed)", circuit_key)
                # fall through to half-open handling below
            else:
                logger.warning(
                    "{} circuit open, skipping "
                    "(failures={}, retry in {:.0f}s)",
                    circuit_key, circuit.failure_count, self._circuit_cooldown - elapsed,
                )
                return False
        # half-open: allow only one probe request at a time
        if circuit_key in self._half_open_probes:
            logger.warning("{} circuit half-open, probe in flight, skipping", circuit_key)
            return False
        self._half_open_probes.add(circuit_key)
        return True

    def _record_success(self, circuit_key: str) -> None:
        circuit = self._get_circuit(circuit_key)
        if circuit.state != "closed":
            logger.info("{} circuit closed (recovered)", circuit_key)
        circuit.failure_count = 0
        circuit.state = "closed"
        self._half_open_probes.discard(circuit_key)

    def _record_failure(self, circuit_key: str) -> None:
        circuit = self._get_circuit(circuit_key)
        circuit.failure_count += 1
        circuit.last_failure_time = time.monotonic()
        self._half_open_probes.discard(circuit_key)
        if circuit.failure_count >= self._circuit_threshold:
            circuit.state = "open"
            logger.warning(
                "{} circuit OPEN ({} consecutive failures)",
                circuit_key, circuit.failure_count,
            )

    def _get_or_create_provider(self, provider_config: ProviderConfig, provider_name: str) -> LLMProvider:
        cache_key = provider_name
        if cache_key not in self._providers_cache:
            from markbot.providers.registry import create_provider, find_by_name

            spec = find_by_name(provider_name)
            backend = spec.backend if spec else "openai_compat"

            self._providers_cache[cache_key] = create_provider(
                backend=backend,
                api_key=provider_config.api_key,
                api_base=provider_config.api_base,
                extra_headers=provider_config.extra_headers,
                spec=spec,
            )

        return self._providers_cache[cache_key]

    @staticmethod
    def _adapt_messages_for_model(
        messages: list[dict[str, Any]],
        provider_name: str,
        model_config: ModelConfig | None,
    ) -> list[dict[str, Any]]:
        """Return a message list adapted to *model_config*'s declared capabilities.

        Today this handles only the vision downgrade: when the target model
        does **not** declare the ``image`` capability and the message stream
        contains ``image_url`` content blocks, replace them with text
        placeholders so the upstream schema accepts the request. The original
        ``messages`` list is never mutated; on no-op we return it as-is.
        """
        if model_config is not None and model_config.has_capability("image"):
            return messages
        if not messages:
            return messages
        try:
            from markbot.providers.base import LLMProvider as _LLMProvider
            stripped = _LLMProvider._strip_image_content(messages)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("image-strip fallback failed for {}: {}", provider_name, exc)
            return messages
        if stripped is None:
            return messages
        # The first non-vision model to attempt the chain will be the one
        # whose attempt fails; we don't pre-strip for the leading models
        # that *do* support images. The log line below makes the downgrade
        # visible in `markbot doctor` / log search.
        logger.info(
            "Downgraded image content to text placeholder for non-vision model {}",
            provider_name,
        )
        return stripped

    # Callable that invokes a specific LLM provider method (chat or chat_stream).
    # The trailing list[dict] is the per-model message list — already adapted for
    # the target model's declared capabilities (e.g. images stripped for non-vision
    # models). The caller MUST forward this list as the ``messages`` argument.
    _ModelCaller = Callable[
        [LLMProvider, ModelConfig, int, float, str | None, list[dict[str, Any]]],
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

            if not self._check_circuit(model_ref):
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

                # Per-model capability adaptation: strip image content for
                # models that do not declare the ``image`` capability. This
                # lets a vision-capable model in the chain keep the image
                # while a downstream text-only model still has a chance to
                # process the conversation (with a text placeholder).
                model_messages = self._adapt_messages_for_model(
                    messages, provider_name, model_config,
                )

                logger.info("Trying model: {}", model_ref)
                response = await caller(
                    provider, model_config, _max_tokens, _temperature, _reasoning, model_messages,
                )

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

                    if response.error_type == ErrorType.TRANSIENT:
                        logger.warning(
                            "Model {} returned error (retryable): {}. Trying next...",
                            model_ref, error_msg,
                        )
                    elif response.error_type == ErrorType.UNAVAILABLE:
                        logger.warning(
                            "Model {} unavailable: {}. Trying next...",
                            model_ref, error_msg,
                        )
                    else:
                        logger.error(
                            "Model {} returned error (non-retryable): {}. Trying next...",
                            model_ref, error_msg,
                        )
                    last_error = Exception(error_msg)
                    # TRANSIENT errors (429/5xx/timeout) are temporary and
                    # should not trip the circuit breaker — only persistent
                    # failures (UNAVAILABLE/UNKNOWN) indicate a real problem.
                    if response.error_type != ErrorType.TRANSIENT:
                        self._record_failure(model_ref)
                    else:
                        # Still release the half-open probe so the next
                        # request can retry this provider.
                        self._half_open_probes.discard(model_ref)
                    continue

                if response.finish_reason == "content_filter":
                    logger.warning(
                        "Model {} returned content_filter, trying next model...",
                        model_ref,
                    )
                    attempt = FallbackAttempt(
                        model_ref=model_ref,
                        provider=provider_config,
                        model=model_config,
                        success=False,
                        error="content_filter",
                    )
                    attempts.append(attempt)
                    last_error = Exception("content_filter")
                    # content_filter is a content-specific refusal, not a
                    # provider outage — the same provider serves other
                    # requests fine. Recording it as a failure would open
                    # the circuit on a healthy provider and take down
                    # every model that shares it. But we MUST still release
                    # the half-open probe so the next request gets a chance
                    # to probe this model — otherwise a single content_filter
                    # during a half-open probe would permanently lock the
                    # model out of the chain.
                    self._half_open_probes.discard(model_ref)
                    continue

                self._record_success(model_ref)
                attempt = FallbackAttempt(
                    model_ref=model_ref,
                    provider=provider_config,
                    model=model_config,
                    success=True,
                    response=response,
                )
                attempts.append(attempt)
                logger.info("Model {} succeeded", model_ref)
                return response, attempts

            except Exception as e:
                attempt = FallbackAttempt(
                    model_ref=model_ref,
                    provider=provider_config,
                    model=model_config,
                    success=False,
                    error=str(e),
                )
                attempts.append(attempt)

                err_type = classify_error(None, str(e))
                if err_type == ErrorType.TRANSIENT:
                    logger.warning(
                        "Model {} failed (retryable): {}. Trying next...",
                        model_ref, e,
                    )
                elif err_type == ErrorType.UNAVAILABLE:
                    logger.warning(
                        "Model {} unavailable: {}. Trying next...", model_ref, e,
                    )
                else:
                    logger.error("Model {} failed (non-retryable): {}. Trying next...", model_ref, e)
                last_error = e
                self._record_failure(model_ref)
                continue

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
            model_messages: list[dict[str, Any]],
        ) -> LLMResponse:
            return await provider.chat_with_retry(
                messages=model_messages,
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
            model_messages: list[dict[str, Any]],
        ) -> LLMResponse:
            return await provider.chat_stream_with_retry(
                messages=model_messages,
                tools=tools,
                model=model_config.name,
                max_tokens=max_tok,
                temperature=temp,
                reasoning_effort=reasoning,
                tool_choice=tool_choice,
                on_content_delta=on_content_delta,
            )

        return await self._try_chain(messages, tools, tool_choice, max_tokens, temperature, _call_stream)
