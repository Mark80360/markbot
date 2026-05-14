from __future__ import annotations

from typing import Any


def default_filter(record: dict[str, Any]) -> bool:
    """Global loguru filter shared by console and file sinks.

    Silences noisy third-party loggers and truncates oversized messages
    from memory-heavy modules.
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
        record["message"] = msg[:2000] + "... [truncated]"

    return True
