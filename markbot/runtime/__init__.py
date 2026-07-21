"""Shared runtime assembly for gateway / web / CLI entrypoints.

Entrypoints declare a :class:`RuntimeFeatures` profile; the factory builds a
consistent :class:`AgentRuntime` (AgentLoop + optional cron/channels/…)
instead of triplicating construction and wiring code.
"""

from markbot.runtime.factory import (
    CLI_FEATURES,
    GATEWAY_FEATURES,
    WEB_FEATURES,
    AgentRuntime,
    RuntimeFeatures,
    build_runtime,
)

__all__ = [
    "AgentRuntime",
    "CLI_FEATURES",
    "GATEWAY_FEATURES",
    "RuntimeFeatures",
    "WEB_FEATURES",
    "build_runtime",
]
