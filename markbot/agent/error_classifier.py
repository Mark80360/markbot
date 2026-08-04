"""Backward-compatible re-export of the API error classifier.

The classifier now lives in :mod:`markbot.providers.error_classifier`
because it belongs to the provider layer (it classifies provider API
errors) and importing it from the agent package dragged the entire agent
core into every provider import chain.

This module is kept as a thin alias so existing importers keep working.
New code should import from ``markbot.providers.error_classifier``.
"""

from __future__ import annotations

from markbot.providers.error_classifier import (  # noqa: F401
    BackoffStrategy,
    ClassifiedError,
    ErrorCategory,
    classify_api_error,
    classify_to_error_type,
)

__all__ = [
    "ErrorCategory",
    "BackoffStrategy",
    "ClassifiedError",
    "classify_api_error",
    "classify_to_error_type",
]
