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


_STATUS_TO_ERROR_TYPE: dict[int, ErrorType] = {
    401: ErrorType.UNAVAILABLE,
    402: ErrorType.UNAVAILABLE,
    403: ErrorType.UNAVAILABLE,
    404: ErrorType.UNAVAILABLE,
    408: ErrorType.TRANSIENT,
    429: ErrorType.TRANSIENT,
    500: ErrorType.TRANSIENT,
    502: ErrorType.TRANSIENT,
    503: ErrorType.TRANSIENT,
    504: ErrorType.TRANSIENT,
    529: ErrorType.TRANSIENT,
}


_ERROR_MESSAGE_HINTS: tuple[tuple[ErrorType, tuple[str, ...]], ...] = (
    (
        ErrorType.TRANSIENT,
        (
            "rate limit", "rate_limit", "too many requests", "throttle",
            "timeout", "timed out", "connection",
            "server error", "internal server error",
            "service unavailable", "overloaded", "capacity", "busy",
            "temporarily unavailable", "try again",
        ),
    ),
    (
        ErrorType.UNAVAILABLE,
        (
            "unauthorized", "invalid api key", "authentication",
            "insufficient balance", "insufficient_quota", "quota exceeded",
            "forbidden", "access denied",
            "model not found", "model_not_found",
            "invalid function arguments", "invalid params",
        ),
    ),
    (
        ErrorType.CONTENT,
        ("content_filter", "content filter"),
    ),
)


def classify_error(status_code: int | None, message: str) -> ErrorType:
    """Classify an LLM error into a structured ``ErrorType``.

    HTTP status_code is the primary signal — when available it is
    authoritative. When ``status_code`` is None (e.g. connection
    timeout, DNS failure, Python exception raised before the HTTP
    response), the function falls back to substring matching on the
    error message. When neither path matches, returns ``ErrorType.UNKNOWN``
    so the caller treats the error as non-retryable rather than retrying
    blindly.
    """
    if status_code is not None and status_code in _STATUS_TO_ERROR_TYPE:
        return _STATUS_TO_ERROR_TYPE[status_code]

    msg_lower = (message or "").lower()
    for error_type, markers in _ERROR_MESSAGE_HINTS:
        if any(marker in msg_lower for marker in markers):
            return error_type
    return ErrorType.UNKNOWN


__all__ = ["ErrorType", "classify_error"]
