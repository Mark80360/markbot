"""Categorical error classification for LLM provider responses.

Replaces the ad-hoc substring-matching of error message text against
``_TRANSIENT_ERROR_MARKERS`` and ``MODEL_UNAVAILABLE_ERRORS`` with a
structured ``ErrorType`` enum. Providers fill in ``LLMResponse.error_type``
at the point where the error is constructed, and callers consume it
via simple ``is``-style checks.
"""

from __future__ import annotations

from enum import Enum


class ErrorType(str, Enum):
    """Categorical error type for an LLM provider response.

    Set on ``LLMResponse.error_type`` whenever ``finish_reason`` is
    ``"error"`` or ``"content_filter"``. ``None`` on successful responses.
    """

    TRANSIENT = "transient"
    """Retryable network/server failure: 429, 5xx, timeout, connection."""

    UNAVAILABLE = "unavailable"
    """The model or account cannot serve the request: 401/402/403/404,
    quota exceeded, model not found, invalid params."""

    CONTENT = "content"
    """The provider returned a content_filter finish reason."""

    UNKNOWN = "unknown"
    """The error did not match any known pattern. Treated as non-retryable."""


def classify_error(status_code: int | None, message: str) -> ErrorType:
    """Classify an LLM error into a structured ``ErrorType``.

    Thin adapter over :func:`markbot.providers.error_classifier.classify_api_error`
    so error semantics have a single source of truth (the shared pattern
    tables live in ``error_classifier``). The one intentional difference is
    the fallback policy for unmatched errors: here an unrecognised error maps
    to ``UNKNOWN`` (non-retryable), while the richer classifier defaults it to
    TRANSIENT (retryable) as the safer choice for the retry loop.

    ``status_code`` is authoritative when present; otherwise the message body
    is matched against the shared patterns.
    """
    # Imported lazily: error_classifier imports ErrorType from this module, so
    # a module-level import would create an import cycle.
    from markbot.providers.error_classifier import classify_api_error

    classified = classify_api_error(status_code, message)
    if classified.reason == "unknown":
        return ErrorType.UNKNOWN
    if classified.reason == "content_policy":
        return ErrorType.CONTENT
    return ErrorType.TRANSIENT if classified.retryable else ErrorType.UNAVAILABLE


__all__ = ["ErrorType", "classify_error"]
