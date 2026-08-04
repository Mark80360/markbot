"""Backward-compatible shim for the Anthropic breakpoint strategy.

The canonical implementation moved to :mod:`markbot.providers.anthropic_breakpoints`
so the providers layer no longer depends on the agent package.  This module
re-exports the same API for existing importers (plugins, notebooks, older code).
"""

from __future__ import annotations

from markbot.providers.anthropic_breakpoints import (
    ANTHROPIC_BREAKPOINT_LIMIT,
    DEFAULT_TTL,
    TRAILING_TOOL_BREAKPOINTS,
    CacheBreakpointSummary,
    attach_system_breakpoints,
    attach_tool_breakpoints,
    attach_user_breakpoint,
    make_cache_control,
    summarise_breakpoints,
    system_and_3,
)

__all__ = [
    "ANTHROPIC_BREAKPOINT_LIMIT",
    "DEFAULT_TTL",
    "TRAILING_TOOL_BREAKPOINTS",
    "CacheBreakpointSummary",
    "attach_system_breakpoints",
    "attach_tool_breakpoints",
    "attach_user_breakpoint",
    "make_cache_control",
    "summarise_breakpoints",
    "system_and_3",
]
