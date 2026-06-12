"""Anthropic prompt-caching breakpoint strategy.

The Anthropic Messages API exposes *explicit* cache breakpoints via
the ``cache_control`` field on a content block::

    {"type": "text", "text": "...", "cache_control": {"type": "ephemeral", "ttl": "5m"}}

A breakpoint marks "everything up to and including this block can be
cached".  Subsequent breakpoints are independent — each one defines
a cacheable span starting at the request root.

## The "system_and_3" strategy

We use four breakpoints, in this order:

  1. End of the **first** system block — caches the mode prompt /
     identity / honesty / security / cache discipline.  This is the
     single most valuable breakpoint: the system prompt is the
     longest byte-stable prefix.
  2. End of the **last** system block (or 1 if only one) — caches
     the remainder of the system prompt (project context, skills,
     reference docs).
  3. The **last 3 tool definitions** — most LLM calls reference
     recently-used tools, and Anthropic's per-block 4-breakpoint
     limit means we concentrate tool breakpoints where they pay
     off the most.
  4. The **last user message** (or assistant message if we just
     finished a turn) — caches the conversation tail so the next
     turn can hit on it.

## TTL

Anthropic supports two TTLs:

  - ``"5m"`` (default) — best for fast-moving sessions; cheaper
    cache reads, shorter retention.
  - ``"1h"`` — for long-running agents where a 1-hour pause is
    common; slightly more expensive writes, but the cache survives
    a coffee break.

We default to ``"5m"`` for the system prompt (rarely changes, the
default works) and accept a configurable ``ttl`` argument for tests
and operators that want to switch.

## References

- Anthropic docs: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Optional, Union


#: Default TTL for cache breakpoints.  5 minutes is the Anthropic default
#: and is the right choice for interactive agents; switch to ``"1h"``
#: for long-running batch workloads.
DEFAULT_TTL: Literal["5m", "1h"] = "5m"


#: Maximum number of breakpoints Anthropic accepts per request.
ANTHROPIC_BREAKPOINT_LIMIT = 4


#: How many trailing tool definitions to mark as cache breakpoints.
#: Anthropic charges per breakpoint and limits to 4 total; with one
#: on the system and one on the user tail, the remaining two go to
#: the last 3 tool definitions (split into two breakpoints that
#: anchor the 2nd- and 3rd-from-last tools).
TRAILING_TOOL_BREAKPOINTS = 2


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

CacheTTL = Literal["5m", "1h"]


def make_cache_control(
    ttl: CacheTTL = DEFAULT_TTL,
    *,
    type_: Literal["ephemeral"] = "ephemeral",
) -> dict[str, str]:
    """Build a ``cache_control`` block for an Anthropic content block."""
    return {"type": type_, "ttl": ttl}


def attach_system_breakpoints(
    system: Union[str, list[dict[str, Any]]],
    *,
    ttl: CacheTTL = DEFAULT_TTL,
) -> Union[str, list[dict[str, Any]]]:
    """Add a ``cache_control`` breakpoint to the last system block.

    If ``system`` is a plain string, return it unchanged — the
    Anthropic API does not accept ``cache_control`` on the legacy
    string form.  Callers that pass a string should convert to
    block form first.
    """
    if not isinstance(system, list) or not system:
        return system
    cc = make_cache_control(ttl)
    out: list[dict[str, Any]] = []
    for i, block in enumerate(system):
        if not isinstance(block, dict):
            out.append(block)
            continue
        new_block = dict(block)
        if i == len(system) - 1:
            new_block["cache_control"] = cc
        out.append(new_block)
    return out


def attach_tool_breakpoints(
    tools: Optional[list[dict[str, Any]]],
    *,
    ttl: CacheTTL = DEFAULT_TTL,
    trailing: int = TRAILING_TOOL_BREAKPOINTS,
) -> Optional[list[dict[str, Any]]]:
    """Mark the trailing ``trailing`` tool definitions with a breakpoint.

    Anthropic allows up to :data:`ANTHROPIC_BREAKPOINT_LIMIT`
    breakpoints per request.  With one used for the system and one
    for the user tail, we have 2 left for tools.  The most recent
    tool definitions are the most likely to be re-used next turn, so
    we concentrate the budget on the tail.
    """
    if not tools or trailing <= 0:
        return tools
    if trailing > ANTHROPIC_BREAKPOINT_LIMIT - 2:
        trailing = ANTHROPIC_BREAKPOINT_LIMIT - 2
    if trailing < 1:
        return tools
    cc = make_cache_control(ttl)
    out: list[dict[str, Any]] = []
    n = len(tools)
    # Mark the last `trailing` tools.  Walking forward so the
    # *trailing-th*-from-last is the earliest breakpoint, the last
    # tool is the latest.
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        new_tool = dict(tool)
        if i >= n - trailing:
            new_tool["cache_control"] = cc
        out.append(new_tool)
    return out


def attach_user_breakpoint(
    messages: list[dict[str, Any]],
    *,
    ttl: CacheTTL = DEFAULT_TTL,
) -> list[dict[str, Any]]:
    """Mark the last user message's last text block with a breakpoint.

    The "tail" breakpoint makes the entire conversation prefix
    (system + history + last user message) cacheable for the next
    turn.  Multimodal / tool blocks are passed through unchanged.
    """
    if not messages:
        return messages
    cc = make_cache_control(ttl)
    out: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if i == len(messages) - 1 and msg.get("role") == "user":
            new_msg = dict(msg)
            content = new_msg.get("content")
            if isinstance(content, str):
                # Convert to a single text block with cache_control.
                new_msg["content"] = [
                    {"type": "text", "text": content, "cache_control": cc}
                ]
            elif isinstance(content, list) and content:
                new_blocks: list[Any] = []
                for j, block in enumerate(content):
                    if not isinstance(block, dict):
                        new_blocks.append(block)
                        continue
                    if j == len(content) - 1 and block.get("type") == "text":
                        new_block = dict(block)
                        new_block["cache_control"] = cc
                        new_blocks.append(new_block)
                    else:
                        new_blocks.append(block)
                new_msg["content"] = new_blocks
            out.append(new_msg)
        else:
            out.append(msg)
    return out


def system_and_3(
    *,
    system: Union[str, list[dict[str, Any]]],
    tools: Optional[list[dict[str, Any]]],
    messages: list[dict[str, Any]],
    ttl: CacheTTL = DEFAULT_TTL,
) -> tuple[Union[str, list[dict[str, Any]]], Optional[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Apply the system_and_3 strategy in one call.

    Returns the (system, tools, messages) triple with the
    appropriate ``cache_control`` blocks attached.  Convenience
    wrapper around the three ``attach_*_breakpoints`` helpers.
    """
    return (
        attach_system_breakpoints(system, ttl=ttl),
        attach_tool_breakpoints(tools, ttl=ttl),
        attach_user_breakpoint(messages, ttl=ttl),
    )


@dataclass
class CacheBreakpointSummary:
    """Diagnostic summary of where breakpoints landed."""

    system_blocks: int
    tool_blocks: int
    user_blocks: int
    ttl: str


def summarise_breakpoints(
    system: Union[str, list[dict[str, Any]]],
    tools: Optional[list[dict[str, Any]]],
    messages: list[dict[str, Any]],
) -> CacheBreakpointSummary:
    """Count ``cache_control`` blocks in the request triple.

    Used by ``/status`` and the cache chip to confirm that the
    expected number of breakpoints landed.
    """
    def _count(items: Optional[Iterable[Any]]) -> int:
        if not items:
            return 0
        n = 0
        for it in items:
            if isinstance(it, dict) and "cache_control" in it:
                n += 1
        return n

    sys_blocks = _count(system if isinstance(system, list) else None)
    tool_blocks = _count(tools)
    user_blocks = 0
    if messages:
        last = messages[-1]
        content = last.get("content")
        if isinstance(content, list):
            user_blocks = _count(content)
    ttl = "5m"
    # Use the first cache_control we find to report the TTL.
    for items in (system if isinstance(system, list) else [], tools or [], messages):
        for it in items:
            if isinstance(it, dict):
                cc = it.get("cache_control")
                if isinstance(cc, dict) and "ttl" in cc:
                    ttl = cc["ttl"]
                    break
        else:
            continue
        break
    return CacheBreakpointSummary(
        system_blocks=sys_blocks,
        tool_blocks=tool_blocks,
        user_blocks=user_blocks,
        ttl=ttl,
    )


__all__ = [
    "DEFAULT_TTL",
    "ANTHROPIC_BREAKPOINT_LIMIT",
    "TRAILING_TOOL_BREAKPOINTS",
    "CacheBreakpointSummary",
    "make_cache_control",
    "attach_system_breakpoints",
    "attach_tool_breakpoints",
    "attach_user_breakpoint",
    "system_and_3",
    "summarise_breakpoints",
]
