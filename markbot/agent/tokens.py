"""Token usage estimation for conversation context.

Provides tiktoken-based estimation for messages, with a simple
char/4 fallback when tiktoken is unavailable.

Includes:
- Image token budget estimation (configurable via env var)
- Conservative padding factor (4/3) for estimation safety margin
- Mixed estimation: uses last API-reported token count + estimates for new messages
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

# The single source of truth for token estimation lives in markbot.utils
# so both the agent loop and the memory manager can use it without a
# cross-module dependency. Re-export here for backward compatibility
# with existing ``from markbot.agent.tokens import estimate_tokens`` calls.
from markbot.utils.tokens import (  # noqa: F401
    estimate_messages_tokens as _estimate_messages_tokens,
    estimate_tokens,
)

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None

TOKEN_ESTIMATION_PADDING = 4 / 3
_DEFAULT_VISION_IMAGE_TOKEN_ESTIMATE = 3_072


def _vision_token_budget_per_image() -> int:
    raw = os.environ.get("MARKBOT_IMAGE_TOKEN_ESTIMATE", "").strip()
    if raw:
        try:
            return max(64, int(raw))
        except ValueError:
            logger.warning("Ignoring invalid MARKBOT_IMAGE_TOKEN_ESTIMATE={}", raw)
    return _DEFAULT_VISION_IMAGE_TOKEN_ESTIMATE


@dataclass
class TokenUsage:
    """Token usage information from API response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
            + self.output_tokens
        )

    @property
    def context_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_tokens": self.total_tokens,
        }


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate token count for a single message using tiktoken.

    Includes proper image token budget estimation and a 4/3 padding
    factor for conservative safety margin.
    """
    content = message.get("content", "")
    parts: list[str] = []
    image_count = 0

    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text is None:
                    text = ""
                parts.append(text)
            elif btype == "image":
                image_count += 1
            elif btype == "tool_use":
                parts.append(json.dumps(block.get("input", {}), ensure_ascii=False))
            elif btype == "tool_result":
                bc = block.get("content")
                if isinstance(bc, str):
                    parts.append(bc)
                elif isinstance(bc, list):
                    for item in bc:
                        if isinstance(item, dict) and item.get("type") == "text":
                            item_text = item.get("text", "")
                            if item_text is None:
                                item_text = ""
                            parts.append(item_text)

    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(value)

    if message.get("tool_calls"):
        parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))

    rc = message.get("reasoning_content")
    if isinstance(rc, str) and rc:
        parts.append(rc)

    payload = "\n".join(parts)
    if not payload and image_count == 0:
        return 4
    if _ENC is not None:
        try:
            text_tokens = max(4, len(_ENC.encode(payload)) + 4) if payload else 4
        except Exception:
            text_tokens = max(4, len(payload) // 4 + 4) if payload else 4
    else:
        text_tokens = max(4, len(payload) // 4 + 4) if payload else 4

    image_tokens = image_count * _vision_token_budget_per_image()
    return int((text_tokens + image_tokens) * TOKEN_ESTIMATION_PADDING)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total token count for a list of messages."""
    return sum(estimate_message_tokens(msg) for msg in messages)


def _get_token_usage_from_message(message: dict[str, Any]) -> Optional[TokenUsage]:
    """Extract token usage from an assistant message (internal helper)."""
    if message.get("role") != "assistant":
        return None
    usage_data = message.get("usage")
    if not usage_data:
        return None
    return TokenUsage(
        input_tokens=usage_data.get("input_tokens", 0),
        output_tokens=usage_data.get("output_tokens", 0),
        cache_creation_input_tokens=usage_data.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=usage_data.get("cache_read_input_tokens", 0),
    )


def token_count_with_estimation(messages: list[dict[str, Any]]) -> int:
    """Estimate the current context size in tokens.

    The returned number is a *mixed* estimate that combines two
    sources of data with different semantics:

    1. **Exact, API-reported total** — from the most recent message
       that carries a ``usage`` block (i.e. an assistant response we
       actually sent to the provider).  ``usage.total_tokens`` is the
       provider's authoritative count for *that* request, including
       input, output, cached, and (for Anthropic) cache-creation
       tokens.  We treat it as the floor of the estimate.

    2. **Padded heuristic** for everything appended *after* that
       response — user/tool messages we have not yet billed.  These
       are run through :func:`estimate_messages_tokens`, which applies
       a conservative 4/3 padding factor on top of the tiktoken
       character estimate to over- rather than under-count.

    The two are summed: ``exact_total + padded_new``.  The result is
    intentionally biased high so the auto-compactor trips *before*
    the provider rejects the request for being too long, rather than
    after.

    .. note::
       The two terms have different units in a strict sense — the
       API total includes the response's *output* tokens, while the
       estimate only covers input-side message bytes — but in
       practice the assistant's output is also persisted into the
       next request's input, so the previous total is a reasonable
       proxy for "where the context window sits now".  When precise
       accounting is required (e.g. for cost reporting), read
       :class:`markbot.agent.cost.CostTracker` directly instead of
       this function.
    """
    last_usage_index = -1
    last_usage = None

    for i in range(len(messages) - 1, -1, -1):
        usage = _get_token_usage_from_message(messages[i])
        if usage:
            last_usage_index = i
            last_usage = usage
            break

    if last_usage:
        new_messages = messages[last_usage_index + 1 :]
        estimated_new = estimate_messages_tokens(new_messages)
        return last_usage.total_tokens + estimated_new

    return estimate_messages_tokens(messages)
