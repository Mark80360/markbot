"""Token usage estimation utilities.

Lightweight helper used by both the agent loop and the memory manager
so context-window decisions can be made in token units (not raw
character counts) without one module depending on the other.

Uses tiktoken (cl100k_base) when available, otherwise falls back to a
char/4 heuristic. Callers should treat the result as an estimate, not
an exact count.
"""

from __future__ import annotations

from typing import Any

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - environment without tiktoken
    _ENC = None


def estimate_tokens(text: str) -> int:
    """Estimate token count for a string.

    Returns 0 for empty input. When tiktoken is unavailable the result
    is ``len(text) // 4`` which is within ~20% of cl100k_base for
    English/CJK mixed content.
    """
    if not text:
        return 0
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:
            pass
    return len(text) // 4


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total token count for a list of chat-style messages.

    Best-effort: sums content strings, includes tool/function arguments
    when present, and pads per message (4 tokens for the role/frame
    overhead) like the OpenAI cookbook heuristic.
    """
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        total += 4  # per-message overhead
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    total += estimate_tokens(block.get("text", "") or "")
                elif btype in ("tool_use", "tool_result"):
                    inner = block.get("input") or block.get("content")
                    if isinstance(inner, str):
                        total += estimate_tokens(inner)
        # Tool-call arguments (OpenAI style)
        if msg.get("tool_calls"):
            import json

            total += estimate_tokens(
                json.dumps(msg["tool_calls"], ensure_ascii=False)
            )
    return total
