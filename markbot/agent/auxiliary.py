"""Unified auxiliary model client for side tasks.

Every side task that needs a cheap LLM (web extraction summarization today;
compression, title generation, session search in the future) shares one
resolution chain instead of duplicating fallback logic per tool.

Resolution (auto mode), first match wins:
  1. Per-task config override — ``agents.defaults.auxiliary.<task>``
     (provider + model) pins that task to an explicit model.
  2. Configured providers (API key set) whose ``ProviderSpec`` declares a
     cheap ``default_aux_model``, in provider registry order.
  3. The main model — ``agents.defaults.model_chain[0]``.

Credit/payment-exhaustion auto-degradation:
  When a resolved provider answers with an error classified as
  ``should_fallback`` (billing / auth / model-not-found / no-channel —
  status 402/432 included), ``AuxTextClient.create()`` automatically retries
  with the next provider in the chain instead of failing the task.

The client exposes an OpenAI-style facade (``client.chat.completions.create``)
so callers integrate with familiar SDK ergonomics.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

from loguru import logger

from markbot.providers.error_classifier import classify_api_error
from markbot.providers.registry import ProviderSpec


class AuxiliaryCallError(RuntimeError):
    """Raised when every provider in the auxiliary chain failed."""


@dataclass(frozen=True)
class _AuxRef:
    """A single resolved step of the auxiliary chain."""

    provider_id: str
    model: str
    spec: ProviderSpec | None = None
    api_key: str = ""
    api_base: str = ""
    extra_headers: dict[str, str] | None = None


class _AuxCompletions:
    def __init__(self, owner: "AuxTextClient") -> None:
        self._owner = owner

    async def create(self, model: str | None = None, messages: list[dict] | None = None, **kwargs: Any) -> SimpleNamespace:
        return await self._owner._create(model=model, messages=messages, kwargs=kwargs)


class _AuxChat:
    def __init__(self, owner: "AuxTextClient") -> None:
        self._owner = owner

    @property
    def completions(self) -> _AuxCompletions:
        return _AuxCompletions(self._owner)


class AuxTextClient:
    """OpenAI-style async client bound to a resolved auxiliary chain.

    Usage: ``client, model = get_async_text_auxiliary_client("web_extract")``
    then ``await client.chat.completions.create(model=model, messages=[...])``.
    """

    def __init__(self, chain: list[_AuxRef], default_model: str) -> None:
        self._chain = list(chain)
        self.default_model = default_model
        self.chat = _AuxChat(self)

    async def _create(
        self,
        model: str | None,
        messages: list[dict] | None,
        kwargs: dict[str, Any],
    ) -> SimpleNamespace:
        if not messages:
            raise AuxiliaryCallError("auxiliary call requires messages")

        chain = list(self._chain)
        if not chain:
            raise AuxiliaryCallError("auxiliary chain is empty")

        # A caller-pinned model (not equal to the resolved default) is tried
        # once against the first provider before the chain takes over.
        if model and model != self.default_model:
            head = chain[0]
            chain = [replace(head, model=model)] + chain[1:]

        max_tokens = kwargs.get("max_tokens") or 4096
        temperature = kwargs.get("temperature")
        last_error: Exception | None = None

        for index, ref in enumerate(chain):
            try:
                provider = _make_provider(ref)
            except Exception as exc:
                last_error = exc
                continue
            try:
                response = await provider.chat_with_retry(
                    messages=messages,
                    tools=None,
                    model=ref.model,
                    max_tokens=max_tokens,
                    temperature=temperature if temperature is not None else 0.7,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Auxiliary provider {} raised: {}", ref.provider_id, str(exc)[:200]
                )
                continue

            if response.finish_reason == "error":
                error_msg = response.content or "unknown auxiliary error"
                last_error = AuxiliaryCallError(error_msg)
                classified = classify_api_error(None, error_msg)
                if classified.should_fallback and index < len(chain) - 1:
                    logger.warning(
                        "Auxiliary provider {} degraded ({}, status={}); "
                        "trying next provider",
                        ref.provider_id, classified.reason, classified.status_code,
                    )
                    continue
                logger.warning(
                    "Auxiliary provider {} failed: {}", ref.provider_id, error_msg[:200]
                )
                continue

            if not response.content:
                last_error = AuxiliaryCallError("empty auxiliary response")
                continue

            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=response.content, text=response.content
                        )
                    )
                ]
            )

        raise AuxiliaryCallError(f"all auxiliary providers failed: {last_error}") from last_error


def _get_config() -> Any:
    try:
        from markbot.config.loader import load_config

        return load_config()
    except Exception:
        return None


def _build_ref(config: Any, provider_id: str, model: str) -> _AuxRef:
    """Build a chain ref for an explicit provider/model reference."""
    from markbot.providers.registry import find_by_name

    spec = find_by_name(provider_id)
    canonical = spec.name if spec else provider_id
    provider_cfg = config.providers.get_provider(canonical)
    keys = provider_cfg.credential_keys if provider_cfg else []
    return _AuxRef(
        provider_id=canonical,
        model=model,
        spec=spec,
        api_key=keys[0] if keys else "",
        api_base=provider_cfg.api_base if provider_cfg else "",
        extra_headers=provider_cfg.extra_headers if provider_cfg else None,
    )


def _task_override(config: Any, task: str) -> _AuxRef | None:
    """Return the explicit per-task pin, or None when not configured."""
    try:
        defaults = config.agents.defaults
        entry = (getattr(defaults, "auxiliary", None) or {}).get(task)
    except Exception:
        return None
    if not entry:
        return None
    provider_id = (getattr(entry, "provider", "") or "").strip()
    model = (getattr(entry, "model", "") or "").strip()
    if not provider_id or not model:
        return None
    return _build_ref(config, provider_id, model)


def _auto_chain(config: Any) -> list[_AuxRef]:
    """Resolve the automatic chain: configured cheap models, then main model."""
    from markbot.providers.registry import find_by_name, list_specs

    refs: list[_AuxRef] = []
    seen: set[tuple[str, str]] = set()

    for spec in list_specs():
        if not spec.default_aux_model:
            continue
        provider_cfg = config.providers.get_provider(spec.name)
        if provider_cfg is None or not provider_cfg.credential_keys:
            continue
        key = (spec.name, spec.default_aux_model)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            _AuxRef(
                provider_id=spec.name,
                model=spec.default_aux_model,
                spec=spec,
                api_key=provider_cfg.credential_keys[0],
                api_base=provider_cfg.api_base,
                extra_headers=provider_cfg.extra_headers,
            )
        )

    # Last resort: the main model from the fallback chain.
    model_chain = config.agents.defaults.model_chain or []
    if model_chain:
        try:
            provider_cfg, model_cfg = config.resolve_model(model_chain[0])
            spec = find_by_name(model_chain[0].split("/")[0])
            key = (model_chain[0].split("/")[0], model_cfg.name)
            if key not in seen:
                keys = provider_cfg.credential_keys
                refs.append(
                    _AuxRef(
                        provider_id=key[0],
                        model=key[1],
                        spec=spec,
                        api_key=keys[0] if keys else "",
                        api_base=provider_cfg.api_base,
                        extra_headers=provider_cfg.extra_headers,
                    )
                )
        except Exception:
            pass

    return refs


def resolve_task_chain(task: str) -> list[tuple[str, str]]:
    """Return the resolved ``(provider_id, model)`` chain for *task*.

    Intended for logging / status display; the first entry is the default.
    """
    config = _get_config()
    if config is None:
        return []
    ref = _task_override(config, task)
    refs = [ref] if ref is not None else _auto_chain(config)
    return [(r.provider_id, r.model) for r in refs]


def get_async_text_auxiliary_client(task: str) -> tuple[AuxTextClient | None, str | None]:
    """Resolve the auxiliary chain for *task* and return ``(client, model)``.

    Returns ``(None, None)`` when nothing usable is configured — callers
    should then fall back to their non-LLM path (e.g. truncation).
    """
    config = _get_config()
    if config is None:
        return None, None
    ref = _task_override(config, task)
    if ref is not None:
        refs = [ref]
    else:
        refs = _auto_chain(config)
    if not refs:
        return None, None
    return AuxTextClient(refs, refs[0].model), refs[0].model


def _make_provider(ref: _AuxRef) -> Any:
    """Instantiate the underlying LLM provider for a chain ref."""
    from markbot.providers.registry import create_provider

    backend = ref.spec.backend if ref.spec else "openai_compat"
    return create_provider(
        backend=backend,
        api_key=ref.api_key or None,
        api_base=ref.api_base or None,
        extra_headers=ref.extra_headers or None,
        spec=ref.spec,
    )


__all__ = [
    "AuxTextClient",
    "AuxiliaryCallError",
    "get_async_text_auxiliary_client",
    "resolve_task_chain",
]