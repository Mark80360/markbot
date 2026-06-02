from __future__ import annotations

from typing import Any

from markbot.log.redact import redact_sensitive


def default_filter(record: dict[str, Any]) -> bool:
    """Global loguru filter shared by console and file sinks.

    Silences noisy third-party loggers, truncates oversized messages
    from memory-heavy modules, and redacts sensitive credentials /
    PII from any message before it is written.
    """
    msg = record["message"]
    name = record["name"]

    if "PING" in msg or "PONG" in msg:
        if "keepalive" in msg.lower() or "websockets" in name:
            return False

    if name.startswith("websockets"):
        return False

    if name.startswith("urllib3") and record["level"].name == "DEBUG":
        return False

    if name.startswith("httpcore") and record["level"].name == "DEBUG":
        return False

    if name == "asyncio" and "selector" in msg.lower():
        return False

    if name.startswith("markbot.memory") and len(msg) > 2000:
        msg = msg[:2000] + "... [truncated]"

    # Always run redaction, even when no truncation happened above.
    # Re-binding ``record["message"]`` follows the project's existing
    # mutate-in-place convention (used by the truncation branch) so
    # downstream sinks see the cleaned text.
    record["message"] = redact_sensitive(msg)

    return True
