"""OpenAI-compatible provider for all non-Anthropic LLM APIs."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import string
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx
import json_repair
from loguru import logger
from openai import AsyncOpenAI

from markbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from markbot.providers.error_classifier import classify_to_error_type
from markbot.providers.errors import (  # noqa: F401  (imported per Task 3 spec)
    ErrorType,
    classify_error,
)

if TYPE_CHECKING:
    from markbot.providers.registry import ProviderSpec

_ALLOWED_MSG_KEYS = frozenset({
    "role", "content", "tool_calls", "tool_call_id", "name",
    "reasoning_content", "extra_content",
})

# Model-name prefixes whose API rejects the legacy ``max_tokens`` request
# field. Matched against the bare model name (any ``vendor/`` prefix and any
# trailing deployment suffix are stripped first), so a routed name like
# ``openai/gpt-5.2-chat`` or an Azure deployment ``my-gpt-5-prod`` is caught
# by the substring rules.
_REASONING_NAME_RE = re.compile(
    r"(?:^|[^a-z0-9])(o[134](?:[-_]|$)|gpt-5|gpt-6|codex)", re.IGNORECASE
)


def _is_reasoning_model(model_name: str) -> bool:
    """Return True when *model_name* is a reasoning model that rejects
    ``max_tokens`` in favour of ``max_completion_tokens``."""
    if not model_name:
        return False
    bare = model_name.strip().lower().split("/")[-1]
    return bool(_REASONING_NAME_RE.search(bare))

_ALNUM = string.ascii_letters + string.digits

_STANDARD_TC_KEYS = frozenset({"id", "type", "index", "function"})
_STANDARD_FN_KEYS = frozenset({"name", "arguments"})


def _short_tool_id() -> str:
    """9-char alphanumeric ID compatible with all providers (incl. Mistral)."""
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


def _get(obj: Any, key: str) -> Any:
    """Get a value from dict or object attribute, returning None if absent."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _coerce_dict(value: Any) -> dict[str, Any] | None:
    """Try to coerce *value* to a dict; return None if not possible or empty."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value if value else None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict) and dumped:
            return dumped
    return None


def _parse_retry_after(e: Exception) -> float | None:
    """Extract the ``Retry-After`` header (seconds) from a provider exception."""
    headers = getattr(getattr(e, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:  # pragma: no cover - defensive: header object shape varies
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def _extract_tc_extras(tc: Any) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Extract (extra_content, provider_specific_fields, fn_provider_specific_fields).

    Works for both SDK objects and dicts.  Captures Gemini ``extra_content``
    verbatim and any non-standard keys on the tool-call / function.
    """
    extra_content = _coerce_dict(_get(tc, "extra_content"))

    tc_dict = _coerce_dict(tc)
    prov = None
    fn_prov = None
    if tc_dict is not None:
        leftover = {k: v for k, v in tc_dict.items()
                    if k not in _STANDARD_TC_KEYS and k != "extra_content" and v is not None}
        if leftover:
            prov = leftover
        fn = _coerce_dict(tc_dict.get("function"))
        if fn is not None:
            fn_leftover = {k: v for k, v in fn.items()
                          if k not in _STANDARD_FN_KEYS and v is not None}
            if fn_leftover:
                fn_prov = fn_leftover
    else:
        prov = _coerce_dict(_get(tc, "provider_specific_fields"))
        fn_obj = _get(tc, "function")
        if fn_obj is not None:
            fn_prov = _coerce_dict(_get(fn_obj, "provider_specific_fields"))

    return extra_content, prov, fn_prov


class OpenAICompatProvider(LLMProvider):
    """Unified provider for all OpenAI-compatible APIs.

    Receives a resolved ``ProviderSpec`` from the caller — no internal
    registry lookups needed.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "gpt-4o",
        extra_headers: dict[str, str] | None = None,
        spec: ProviderSpec | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self._spec = spec

        if api_key and spec and spec.env_key:
            self._setup_env(api_key, api_base)

        effective_base = api_base or (spec.default_api_base if spec else None) or None

        self._client = AsyncOpenAI(
            api_key=api_key or "no-key",
            base_url=effective_base,
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
            default_headers={
                "x-session-affinity": uuid.uuid4().hex,
                **(extra_headers or {}),
            },
        )

    def _setup_env(self, api_key: str, api_base: str | None) -> None:
        """Set environment variables based on provider spec.

        Always ``setdefault``: a key already present in the process
        environment was put there by the operator (or by a sibling
        provider) and silently overwriting it here would repoint a
        correctly-configured client at a different credential. Child
        processes spawned by the shell tool inherit ``os.environ`` in
        full, so an unexpected overwrite here is a credential leak.
        """
        spec = self._spec
        if not spec or not spec.env_key:
            return
        os.environ.setdefault(spec.env_key, api_key)
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key).replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    @staticmethod
    def _apply_cache_control(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Inject cache_control markers for prompt caching."""
        cache_marker = {"type": "ephemeral"}
        new_messages = list(messages)

        def _mark(msg: dict[str, Any]) -> dict[str, Any]:
            content = msg.get("content")
            if isinstance(content, str):
                return {**msg, "content": [
                    {"type": "text", "text": content, "cache_control": cache_marker},
                ]}
            if isinstance(content, list) and content:
                nc = list(content)
                nc[-1] = {**nc[-1], "cache_control": cache_marker}
                return {**msg, "content": nc}
            return msg

        if new_messages and new_messages[0].get("role") == "system":
            new_messages[0] = _mark(new_messages[0])
        if len(new_messages) >= 3:
            new_messages[-2] = _mark(new_messages[-2])

        new_tools = tools
        if tools:
            new_tools = list(tools)
            new_tools[-1] = {**new_tools[-1], "cache_control": cache_marker}
        return new_messages, new_tools

    @staticmethod
    def _normalize_tool_call_id(tool_call_id: Any) -> Any:
        """Normalize to a provider-safe 9-char alphanumeric form."""
        if not isinstance(tool_call_id, str):
            return tool_call_id
        if len(tool_call_id) == 9 and tool_call_id.isalnum():
            return tool_call_id
        return hashlib.sha1(tool_call_id.encode()).hexdigest()[:9]

    def _sanitize_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip non-standard keys, normalize tool_call IDs."""
        sanitized = LLMProvider._sanitize_request_messages(messages, _ALLOWED_MSG_KEYS)
        id_map: dict[str, str] = {}

        def map_id(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            return id_map.setdefault(value, self._normalize_tool_call_id(value))

        for clean in sanitized:
            if isinstance(clean.get("tool_calls"), list):
                normalized = []
                for tc in clean["tool_calls"]:
                    if not isinstance(tc, dict):
                        normalized.append(tc)
                        continue
                    tc_clean = dict(tc)
                    tc_clean["id"] = map_id(tc_clean.get("id"))
                    normalized.append(tc_clean)
                clean["tool_calls"] = normalized
            if "tool_call_id" in clean and clean["tool_call_id"]:
                clean["tool_call_id"] = map_id(clean["tool_call_id"])
        return sanitized

    # ------------------------------------------------------------------
    # Build kwargs
    # ------------------------------------------------------------------

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        from markbot.providers.registry import _OMIT_TEMPERATURE

        model_name = model or self.default_model
        spec = self._spec

        if spec and spec.supports_prompt_caching:
            messages, tools = self._apply_cache_control(messages, tools)

        if spec and spec.strip_model_prefix:
            model_name = model_name.split("/")[-1]

        if spec:
            messages = spec.prepare_messages(messages)

        sanitized = self._sanitize_messages(self._sanitize_empty_content(messages))

        # OpenAI's reasoning models (gpt-5, o1/o3/o4-mini, …) reject the
        # legacy ``max_tokens`` field outright with
        # ``400 Unsupported parameter``. Every other model still wants it.
        # ``max_completion_tokens`` is understood by both generations, so it
        # is always safe to send; ``max_tokens`` is only added when the model
        # is not a known reasoning model.
        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": sanitized,
            "max_completion_tokens": max(1, max_tokens),
        }
        if not _is_reasoning_model(model_name):
            kwargs["max_tokens"] = max(1, max_tokens)

        if spec and spec.fixed_temperature is _OMIT_TEMPERATURE:
            pass
        elif spec and spec.fixed_temperature is not None:
            kwargs["temperature"] = spec.fixed_temperature
        else:
            kwargs["temperature"] = temperature

        if spec:
            model_lower = model_name.lower()
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    break

        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        if spec:
            reasoning_config = None
            if reasoning_effort:
                reasoning_config = {"enabled": True, "effort": reasoning_effort}

            extra_body_from_spec = spec.build_extra_body(
                model=model_name,
                reasoning_config=reasoning_config,
            )
            if extra_body_from_spec:
                existing = kwargs.get("extra_body", {})
                if isinstance(existing, dict):
                    existing.update(extra_body_from_spec)
                else:
                    existing = extra_body_from_spec
                kwargs["extra_body"] = existing

            eb_additions, top_level = spec.build_api_kwargs_extras(
                reasoning_config=reasoning_config,
                model=model_name,
            )
            if eb_additions:
                existing = kwargs.get("extra_body", {})
                if isinstance(existing, dict):
                    existing.update(eb_additions)
                else:
                    existing = eb_additions
                kwargs["extra_body"] = existing
            if top_level:
                kwargs.update(top_level)

            if spec.default_max_tokens and "max_tokens" not in kwargs:
                kwargs["max_tokens"] = spec.default_max_tokens
                kwargs["max_completion_tokens"] = spec.default_max_tokens
        return kwargs

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _maybe_mapping(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        return None

    @classmethod
    def _extract_text_content(cls, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                item_map = cls._maybe_mapping(item)
                if item_map:
                    text = item_map.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                        continue
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
                    continue
                if isinstance(item, str):
                    parts.append(item)
            return "".join(parts) or None
        return str(value)

    @classmethod
    def _extract_usage(cls, response: Any) -> dict[str, int]:
        usage_obj = None
        response_map = cls._maybe_mapping(response)
        if response_map is not None:
            usage_obj = response_map.get("usage")
        elif hasattr(response, "usage") and response.usage:
            usage_obj = response.usage

        usage_map = cls._maybe_mapping(usage_obj)
        if usage_map is not None:
            pt = int(usage_map.get("prompt_tokens") or 0)
            ct = int(usage_map.get("completion_tokens") or 0)
            tt = int(usage_map.get("total_tokens") or 0)
            result = {
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
                "input_tokens": int(usage_map.get("input_tokens") or pt),
                "output_tokens": int(usage_map.get("output_tokens") or ct),
                # Anthropic-style cache fields
                "cache_creation_input_tokens": int(usage_map.get("cache_creation_input_tokens") or 0),
                "cache_read_input_tokens": int(usage_map.get("cache_read_input_tokens") or 0),
                # DeepSeek / OpenAI-style cache fields. Only emitted when the
                # provider actually reported them: a hard 0 here is
                # indistinguishable from "zero misses", and downstream that
                # reads as a real miss count — which would bill the whole
                # prompt as cache-hit and charge nothing for input.
                "prompt_cache_hit_tokens": int(usage_map.get("prompt_cache_hit_tokens") or 0),
                "prompt_cache_miss_tokens": int(usage_map["prompt_cache_miss_tokens"])
                if usage_map.get("prompt_cache_miss_tokens") is not None else None,
                # OpenAI Codex Responses-style cache fields
                "cached_tokens": int(usage_map.get("cached_tokens") or 0),
            }
            # A None value is not a valid usage field; drop the key so the
            # normaliser sees "absent" rather than an explicit null.
            return {k: v for k, v in result.items() if v is not None}

        if usage_obj:
            pt = getattr(usage_obj, "prompt_tokens", 0) or 0
            ct = getattr(usage_obj, "completion_tokens", 0) or 0
            tt = getattr(usage_obj, "total_tokens", 0) or 0
            result = {
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
                "input_tokens": getattr(usage_obj, "input_tokens", 0) or pt,
                "output_tokens": getattr(usage_obj, "output_tokens", 0) or ct,
                # Anthropic-style cache fields
                "cache_creation_input_tokens": getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
                # DeepSeek / OpenAI-style cache fields. See the note in the
                # dict branch above: a synthesised 0 is not "zero misses".
                "prompt_cache_hit_tokens": getattr(usage_obj, "prompt_cache_hit_tokens", 0) or 0,
                "prompt_cache_miss_tokens": int(getattr(usage_obj, "prompt_cache_miss_tokens", 0) or 0)
                if getattr(usage_obj, "prompt_cache_miss_tokens", None) is not None else None,
                # OpenAI Codex Responses-style cache fields
                "cached_tokens": getattr(usage_obj, "cached_tokens", 0) or 0,
            }
            return {k: v for k, v in result.items() if v is not None}
        return {}

    def _parse(self, response: Any) -> LLMResponse:
        if isinstance(response, str):
            return LLMResponse(content=response, finish_reason="stop")

        response_map = self._maybe_mapping(response)
        if response_map is not None:
            choices = response_map.get("choices") or []
            if not choices:
                content = self._extract_text_content(
                    response_map.get("content") or response_map.get("output_text")
                )
                if content is not None:
                    return LLMResponse(
                        content=content,
                        finish_reason=str(response_map.get("finish_reason") or "stop"),
                        usage=self._extract_usage(response_map),
                    )
                return LLMResponse(
                    content="Error: API returned empty choices.",
                    finish_reason="error",
                    error_type=ErrorType.UNKNOWN,
                )

            choice0 = self._maybe_mapping(choices[0]) or {}
            msg0 = self._maybe_mapping(choice0.get("message")) or {}
            content = self._extract_text_content(msg0.get("content"))
            finish_reason = str(choice0.get("finish_reason") or "stop")

            raw_tool_calls: list[Any] = []
            reasoning_content = msg0.get("reasoning_content")
            for ch in choices:
                ch_map = self._maybe_mapping(ch) or {}
                m = self._maybe_mapping(ch_map.get("message")) or {}
                tool_calls = m.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    raw_tool_calls.extend(tool_calls)
                    if ch_map.get("finish_reason") in ("tool_calls", "stop"):
                        finish_reason = str(ch_map["finish_reason"])
                if not content:
                    content = self._extract_text_content(m.get("content"))
                if not reasoning_content:
                    reasoning_content = m.get("reasoning_content")

            parsed_tool_calls = []
            for tc in raw_tool_calls:
                tc_map = self._maybe_mapping(tc) or {}
                fn = self._maybe_mapping(tc_map.get("function")) or {}
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    args = json_repair.loads(args)
                ec, prov, fn_prov = _extract_tc_extras(tc)
                parsed_tool_calls.append(ToolCallRequest(
                    id=_short_tool_id(),
                    name=str(fn.get("name") or ""),
                    arguments=args if isinstance(args, dict) else {},
                    extra_content=ec,
                    provider_specific_fields=prov,
                    function_provider_specific_fields=fn_prov,
                ))

            return LLMResponse(
                content=content,
                tool_calls=parsed_tool_calls,
                finish_reason=finish_reason,
                usage=self._extract_usage(response_map),
                reasoning_content=reasoning_content if isinstance(reasoning_content, str) else None,
            )

        if not response.choices:
            return LLMResponse(
                content="Error: API returned empty choices.",
                finish_reason="error",
                error_type=ErrorType.UNKNOWN,
            )

        choice = response.choices[0]
        msg = choice.message
        content = msg.content
        finish_reason = choice.finish_reason

        raw_tool_calls: list[Any] = []
        for ch in response.choices:
            m = ch.message
            if hasattr(m, "tool_calls") and m.tool_calls:
                raw_tool_calls.extend(m.tool_calls)
                if ch.finish_reason in ("tool_calls", "stop"):
                    finish_reason = ch.finish_reason
            if not content and m.content:
                content = m.content

        tool_calls = []
        for tc in raw_tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                args = json_repair.loads(args)
            ec, prov, fn_prov = _extract_tc_extras(tc)
            tool_calls.append(ToolCallRequest(
                id=_short_tool_id(),
                name=tc.function.name,
                arguments=args,
                extra_content=ec,
                provider_specific_fields=prov,
                function_provider_specific_fields=fn_prov,
            ))

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            usage=self._extract_usage(response),
            reasoning_content=getattr(msg, "reasoning_content", None) or None,
        )

    @classmethod
    def _parse_chunks(cls, chunks: list[Any]) -> LLMResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tc_bufs: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: dict[str, int] = {}

        def _accum_tc(tc: Any, idx_hint: int) -> None:
            """Accumulate one streaming tool-call delta into *tc_bufs*."""
            tc_index: int = _get(tc, "index") if _get(tc, "index") is not None else idx_hint
            buf = tc_bufs.setdefault(tc_index, {
                "id": "", "name": "", "arguments": "",
                "extra_content": None, "prov": None, "fn_prov": None,
            })
            tc_id = _get(tc, "id")
            if tc_id:
                buf["id"] = str(tc_id)
            fn = _get(tc, "function")
            if fn is not None:
                fn_name = _get(fn, "name")
                if fn_name:
                    buf["name"] = str(fn_name)
                fn_args = _get(fn, "arguments")
                if fn_args:
                    buf["arguments"] += str(fn_args)
            ec, prov, fn_prov = _extract_tc_extras(tc)
            if ec:
                buf["extra_content"] = ec
            if prov:
                buf["prov"] = prov
            if fn_prov:
                buf["fn_prov"] = fn_prov

        def _accum_reasoning(value: Any) -> None:
            """Append one reasoning delta (string or list) to the accumulator."""
            if value is None:
                return
            if isinstance(value, str):
                if value:
                    reasoning_parts.append(value)
                return
            if isinstance(value, list):
                for part in value:
                    if isinstance(part, str):
                        if part:
                            reasoning_parts.append(part)
                        continue
                    part_map = cls._maybe_mapping(part)
                    if part_map is not None:
                        text = part_map.get("text") or part_map.get("reasoning")
                        if isinstance(text, str) and text:
                            reasoning_parts.append(text)
                return
            # Some OpenAI-compatible SDKs expose nested objects
            text = getattr(value, "text", None) or getattr(value, "reasoning", None)
            if isinstance(text, str) and text:
                reasoning_parts.append(text)

        for chunk in chunks:
            if isinstance(chunk, str):
                content_parts.append(chunk)
                continue

            chunk_map = cls._maybe_mapping(chunk)
            if chunk_map is not None:
                choices = chunk_map.get("choices") or []
                if not choices:
                    usage = cls._extract_usage(chunk_map) or usage
                    text = cls._extract_text_content(
                        chunk_map.get("content") or chunk_map.get("output_text")
                    )
                    if text:
                        content_parts.append(text)
                    # Some providers surface reasoning at the top level on
                    # the final chunk.
                    _accum_reasoning(chunk_map.get("reasoning_content"))
                    continue
                choice = cls._maybe_mapping(choices[0]) or {}
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                delta = cls._maybe_mapping(choice.get("delta")) or {}
                text = cls._extract_text_content(delta.get("content"))
                if text:
                    content_parts.append(text)
                # Streamed reasoning_content arrives as a delta field.
                _accum_reasoning(delta.get("reasoning_content"))
                for idx, tc in enumerate(delta.get("tool_calls") or []):
                    _accum_tc(tc, idx)
                usage = cls._extract_usage(chunk_map) or usage
                continue

            if not chunk.choices:
                usage = cls._extract_usage(chunk) or usage
                # Some SDKs surface reasoning_content on usage-chunk.
                _accum_reasoning(getattr(chunk, "reasoning_content", None))
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta and delta.content:
                content_parts.append(delta.content)
            # Streamed reasoning_content (DeepSeek / Kimi / etc.).
            if delta is not None:
                _accum_reasoning(getattr(delta, "reasoning_content", None))
            for tc in (delta.tool_calls or []) if delta else []:
                _accum_tc(tc, getattr(tc, "index", 0))

        if finish_reason == "content_filter":
            content_parts = []

        reasoning_content: str | None = "".join(reasoning_parts) if reasoning_parts else None

        return LLMResponse(
            content="".join(content_parts) or "",
            tool_calls=[
                ToolCallRequest(
                    id=b["id"] or _short_tool_id(),
                    name=b["name"],
                    arguments=json_repair.loads(b["arguments"]) if b["arguments"] else {},
                    extra_content=b.get("extra_content"),
                    provider_specific_fields=b.get("prov"),
                    function_provider_specific_fields=b.get("fn_prov"),
                )
                for b in tc_bufs.values()
            ],
            finish_reason=finish_reason,
            usage=usage,
            reasoning_content=reasoning_content,
            error_type=ErrorType.CONTENT if finish_reason == "content_filter" else None,
        )

    @staticmethod
    def _handle_error(e: Exception) -> LLMResponse:
        body = getattr(e, "doc", None) or getattr(getattr(e, "response", None), "text", None)
        if body and body.strip():
            msg = f"Error: {body.strip()[:500]}"
        elif str(e).strip():
            msg = f"Error calling LLM: {e}"
        else:
            msg = "Error calling LLM: connection failed or timed out (empty error from provider)"
        logger.error("Provider error: {}: {}", type(e).__name__, e)
        status_code = getattr(e, "status_code", None)
        return LLMResponse(
            content=msg,
            finish_reason="error",
            error_type=classify_to_error_type(status_code, repr(e), e),
            status_code=status_code,
            retry_after=_parse_retry_after(e),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        kwargs = self._build_kwargs(
            messages, tools, model, max_tokens, temperature,
            reasoning_effort, tool_choice,
        )
        try:
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as e:
            return self._handle_error(e)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        kwargs = self._build_kwargs(
            messages, tools, model, max_tokens, temperature,
            reasoning_effort, tool_choice,
        )
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        try:
            stream = await asyncio.wait_for(
                self._client.chat.completions.create(**kwargs),
                timeout=30.0,
            )
            chunks: list[Any] = []
            async for chunk in stream:
                chunks.append(chunk)
                if on_content_delta and chunk.choices:
                    text = getattr(chunk.choices[0].delta, "content", None)
                    if text:
                        await on_content_delta(text)
            return self._parse_chunks(chunks)
        except Exception as e:
            # Stop emitting deltas for this attempt. `finish_reason == "error"`
            # makes the fallback chain rotate to the next model, and that
            # model streams into the *same* sink — so partial text from this
            # attempt would be concatenated with the replacement's full
            # output, and neither can be retracted. The loop already treats
            # `content_filter` this way (it discards the stream buffer); a
            # mid-stream transport failure needs the same treatment.
            return self._handle_error(e)

    def get_default_model(self) -> str:
        return self.default_model


# Imported at the bottom deliberately: registry imports the provider classes
# to build its factory table, so a top-level import here would be circular.
from markbot.providers.registry import register_provider_factory  # noqa: E402

register_provider_factory("openai_compat", OpenAICompatProvider)
