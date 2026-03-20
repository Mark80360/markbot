"""Shared sanitization utilities."""

import json
from typing import Any

from loguru import logger


def _sanitize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize tool_calls to ensure arguments are valid JSON objects."""
    sanitized = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function", {})
        if not func:
            continue
        args = func.get("arguments")
        if args is None:
            continue
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                if isinstance(parsed, dict):
                    func["arguments"] = parsed
                else:
                    logger.warning(
                        "Tool call '{}' has non-object arguments (type: {}), skipping",
                        func.get("name"),
                        type(parsed).__name__,
                    )
                    continue
            except json.JSONDecodeError:
                logger.warning(
                    "Tool call '{}' has invalid JSON arguments: {}, skipping",
                    func.get("name"),
                    args[:100],
                )
                continue
        elif isinstance(args, list):
            logger.warning(
                "Tool call '{}' has array arguments (should be object), skipping", func.get("name")
            )
            continue
        elif not isinstance(args, dict):
            logger.warning(
                "Tool call '{}' has invalid arguments type: {}, skipping",
                func.get("name"),
                type(args).__name__,
            )
            continue
        sanitized.append(tc)
    return sanitized
