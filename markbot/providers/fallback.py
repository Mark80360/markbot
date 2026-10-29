"""Multi-model fallback chain management."""

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from markbot.config.schema import Config, ModelConfig, ProviderConfig
from markbot.errors import ErrorCode, MarkbotError, classify_exception
from markbot.providers.base import LLMProvider, LLMResponse
from markbot.providers.error_classifier import classify_api_error, classify_to_error_type
from markbot.providers.errors import ErrorType


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
    """Record of a single fallback attempt.

    ``error_code`` carries the machine-readable classification from
    :mod:`markbot.errors` so telemetry / retry policy can dispatch on
    codes instead of parsing ``error`` prose.
    """

    model_ref: str
    provider: ProviderConfig | None = None
    model: ModelConfig | None = None
    success: bool = False
    error: str | None = None
    response: LLMResponse | None = None
    circuit_skipped: bool = False
    error_code: ErrorCode | None = None
    #: True once this attempt has pushed content deltas to the user. Emitted
    #: bytes cannot be retracted, so a later attempt that streams into the
    #: same sink must know the buffer is already polluted and force a
    #: discard instead of concatenating two answers.
    streamed: bool = False


class AllModelsFailedError(MarkbotError):
    """Raised when all models in the chain have failed."""

    default_code = ErrorCode.ALL_MODELS_FAILED

    def __init__(
        self,
        message: str,
        attempts: list[FallbackAttempt] | None = None,
        last_error: Exception | None = None,
    ):
        super().__init__(message, code=ErrorCode.ALL_MODELS_FAILED, cause=last_error)
        self.attempts = attempts or []
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

    def _record_failure(self, circuit_key: str, *, permanent: bool = False) -> None:
        """Record a failure and open the circuit when warranted.

        ``permanent=True`` marks a deterministic failure (model not found, no
        available channel, auth/billing) that retrying cannot fix, so the
        circuit opens immediately rather than waiting for the consecutive-
        failure threshold — a genuinely dead model is ejected after a single
        response instead of being probed ``threshold`` times per cooldown.
        """
        circuit = self._get_circuit(circuit_key)
        circuit.failure_count += 1
        circuit.last_failure_time = time.monotonic()
        self._half_open_probes.discard(circuit_key)
        if permanent or circuit.failure_count >= self._circuit_threshold:
            if circuit.state != "open":
                circuit.state = "open"
                logger.warning(
                    "{} circuit OPEN ({} consecutive failures{})",
                    circuit_key, circuit.failure_count,
                    ", permanent error" if permanent else "",
                )

    def _get_or_create_provider(
        self,
        provider_config: ProviderConfig,
        provider_name: str,
        api_key: str | None = None,
        credential_variant: int = 0,
    ) -> LLMProvider:
        """Get (or build + cache) a provider for the given credential slot.

        ``credential_variant == 0`` is the canonical provider cached under
        ``provider_name``; higher variants come from ``ProviderConfig.api_keys``
        and are cached under a suffixed key so every credential gets its own
        client instance (API key is fixed at construction time).
        """
        cache_key = (
            provider_name
            if credential_variant == 0
            else f"{provider_name}::credential{credential_variant}"
        )
        if cache_key not in self._providers_cache:
            from markbot.providers.registry import create_provider, find_by_name

            spec = find_by_name(provider_name)
            backend = spec.backend if spec else "openai_compat"

            self._providers_cache[cache_key] = create_provider(
                backend=backend,
                api_key=api_key or None,
                api_base=provider_config.api_base,
                extra_headers=provider_config.extra_headers,
                spec=spec,
            )

        provider = self._providers_cache[cache_key]
        # Apply the per-provider TLS downgrade opt-in, which lives in
        # config rather than on the (static) provider spec.
        insecure = bool(getattr(provider_config, "insecure_skip_verify", False))
        if hasattr(provider, "insecure_skip_verify"):
            provider.insecure_skip_verify = insecure
        return provider

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
                credential_keys = provider_config.credential_keys or [""]

                _max_tokens = max_tokens
                if _max_tokens is None:
                    _max_tokens = model_config.max_tokens or defaults.max_tokens

                # NOTE: ``is None`` (not ``or``) — an explicit temperature of
                # 0.0 or 0 max_tokens is a valid setting and must not be
                # silently replaced by the default.
                _temperature = temperature
                if _temperature is None:
                    _temperature = (
                        model_config.temperature
                        if model_config.temperature is not None
                        else defaults.temperature
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

                # Try every credential for this model before giving up and
                # moving to the next model in the chain. Credential-specific
                # failures — per-key rate limits (429), credit exhaustion
                # (402/432), auth failures — rotate to the next credential,
                # so a healthy model with one dead key does not trip its
                # circuit breaker.
                last_error: Exception | None = None
                content_filter_hit = False
                # HTTP status of the most recent failure, carried from the
                # provider so the chain re-classifies with the authoritative
                # status instead of guessing from the error prose alone.
                error_status: int | None = None
                for key_index, credential in enumerate(credential_keys):
                    provider = self._get_or_create_provider(
                        provider_config, provider_name,
                        api_key=credential, credential_variant=key_index,
                    )
                    logger.info(
                        "Trying model: {} (credential {}/{})",
                        model_ref, key_index + 1, len(credential_keys),
                    )
                    try:
                        response = await caller(
                            provider, model_config, _max_tokens, _temperature, _reasoning, model_messages,
                        )
                    except Exception as e:
                        last_error = e
                        error_status = getattr(e, "status_code", None)
                        if key_index < len(credential_keys) - 1:
                            logger.warning(
                                "Model {} threw with credential {}/{}: {}. "
                                "Rotating to next credential...",
                                model_ref, key_index + 1, len(credential_keys), e,
                            )
                            continue
                        break

                    if response.finish_reason == "error":
                        error_msg = response.content or "Unknown error"
                        error_status = response.status_code
                        classified = classify_api_error(error_status, error_msg)
                        last_error = Exception(error_msg)
                        if key_index < len(credential_keys) - 1 and (
                            classified.retryable or classified.should_fallback
                        ):
                            logger.warning(
                                "Model {} error ({}): rotating to next credential",
                                model_ref, classified.reason,
                            )
                            continue
                        break

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
                        content_filter_hit = True
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
                        break

                    self._record_success(model_ref)
                    attempt = FallbackAttempt(
                        model_ref=model_ref,
                        provider=provider_config,
                        model=model_config,
                        success=True,
                        response=response,
                    )
                    attempts.append(attempt)
                    logger.info(
                        "Model {} succeeded (credential {}/{})",
                        model_ref, key_index + 1, len(credential_keys),
                    )
                    return response, attempts

                # Every credential for this model failed — record one attempt
                # with the last error, then move to the next model.
                if content_filter_hit:
                    # attempt already recorded inline; move to next model
                    continue
                if last_error is not None:
                    error_msg = str(last_error)
                    attempt = FallbackAttempt(
                        model_ref=model_ref,
                        provider=provider_config,
                        model=model_config,
                        success=False,
                        error=error_msg,
                        error_code=classify_exception(last_error).code,
                    )
                    attempts.append(attempt)

                    classification = classify_api_error(
                        error_status, error_msg, last_error
                    )
                    err_type = classify_to_error_type(error_status, error_msg, last_error)
                    if err_type == ErrorType.TRANSIENT:
                        logger.warning(
                            "Model {} failed (retryable): {}. Trying next...",
                            model_ref, error_msg,
                        )
                    elif err_type == ErrorType.UNAVAILABLE:
                        logger.warning(
                            "Model {} unavailable: {}. Trying next...",
                            model_ref, error_msg,
                        )
                    else:
                        logger.error(
                            "Model {} failed (non-retryable): {}. Trying next...",
                            model_ref, error_msg,
                        )
                    # TRANSIENT errors (429/5xx/timeout) are temporary and
                    # should not trip the circuit breaker — only persistent
                    # failures (UNAVAILABLE/UNKNOWN) indicate a real problem.
                    # UNAVAILABLE is deterministic (model not found, no
                    # channel, auth/billing) so it opens the circuit at once.
                    if err_type != ErrorType.TRANSIENT:
                        # A context overflow is deterministic for *this*
                        # request, not for the model: compacting and retrying
                        # is the intended recovery, so do not open the
                        # circuit and lock the model out for every session.
                        self._record_failure(
                            model_ref,
                            permanent=(
                                err_type == ErrorType.UNAVAILABLE
                                and not classification.should_compress
                            ),
                        )
                    else:
                        # Still release the half-open probe so the next
                        # request can retry this provider.
                        self._half_open_probes.discard(model_ref)
                continue

            except Exception as e:
                # Infrastructure / resolution failure (e.g. invalid model
                # reference in the chain) — record and move to next model.
                attempt = FallbackAttempt(
                    model_ref=model_ref,
                    provider=provider_config,
                    model=model_config,
                    success=False,
                    error=str(e),
                    error_code=classify_exception(e).code,
                )
                attempts.append(attempt)

                classification = classify_api_error(
                    getattr(e, "status_code", None), str(e), e
                )
                err_type = classify_to_error_type(getattr(e, "status_code", None), str(e), e)
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
                self._record_failure(
                    model_ref,
                    permanent=(
                        err_type == ErrorType.UNAVAILABLE
                        and not classification.should_compress
                    ),
                )
                continue
            finally:
                # Always release the half-open probe. ``except Exception`` above
                # does NOT catch ``asyncio.CancelledError`` (a BaseException),
                # so without this a single cancelled request would leave the
                # probe "in flight" and lock the model out permanently.
                self._half_open_probes.discard(model_ref)

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

        # Track whether this attempt has pushed bytes to the user. Emitted
        # deltas cannot be retracted, so if the attempt then fails and the
        # chain rotates, the next model must be told the buffer is polluted
        # rather than concatenating two answers for the user.
        emitted = {"any": False}

        if on_content_delta is not None:
            _inner = on_content_delta

            async def _tracking(text: str) -> None:
                emitted["any"] = True
                await _inner(text)

            on_content_delta = _tracking

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

        response, attempts = await self._try_chain(
            messages, tools, tool_choice, max_tokens, temperature, _call_stream,
        )
        # Attribute the emission flag to every attempt that shares this sink
        # and failed, so the loop can discard the buffer.
        if emitted["any"]:
            for a in attempts:
                if not a.success:
                    a.streamed = True
        return response, attempts
