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

# Body-level markers that indicate a PERMANENT failure. These take
# precedence over status code because aggregators (new_api / one-api
# style distributors) often return 5xx with a body that reveals the
# true failure is permanent — e.g. ``model_not_found`` or
# ``no available channel`` on a 503. Treating such responses as
# TRANSIENT (based on the 5xx status alone) causes the fallback chain
# to retry each dead model 3 times, wasting ~10s per model.
_UNAVAILABLE_BODY_MARKERS: tuple[str, ...] = (
    "unauthorized", "invalid api key", "authentication",
    "insufficient balance", "insufficient_quota", "quota exceeded",
    "forbidden", "access denied",
    "model not found", "model_not_found",
    "no available channel",
    "invalid function arguments", "invalid params",
)

_CONTENT_BODY_MARKERS: tuple[str, ...] = (
    "content_filter", "content filter",
)

_TRANSIENT_BODY_MARKERS: tuple[str, ...] = (
    "rate limit", "rate_limit", "too many requests", "throttle",
    "timeout", "timed out", "connection",
    "server error", "internal server error",
    "service unavailable", "overloaded", "capacity", "busy",
    "temporarily unavailable", "try again",
)


def classify_error(status_code: int | None, message: str) -> ErrorType:
    """Classify an LLM error into a structured ``ErrorType``.

    Resolution order (most-specific first):

    1. **Permanent body markers** (``model_not_found``, ``unauthorized``,
       ``quota exceeded`` …). Body semantics win over status code because
       aggregators often proxy permanent failures through 5xx responses.
    2. **Content-filter body markers**.
    3. **HTTP status code** — authoritative for transient network/server
       failures when the body has no permanent marker.
    4. **Transient body markers** (``timeout``, ``rate limit`` …) — only
       consulted when there is no status code (e.g. connection-level
       exceptions raised before an HTTP response).
    5. ``ErrorType.UNKNOWN``.
    """
    msg_lower = (message or "").lower()

    for marker in _UNAVAILABLE_BODY_MARKERS:
        if marker in msg_lower:
            return ErrorType.UNAVAILABLE
    for marker in _CONTENT_BODY_MARKERS:
        if marker in msg_lower:
            return ErrorType.CONTENT

    if status_code is not None and status_code in _STATUS_TO_ERROR_TYPE:
        return _STATUS_TO_ERROR_TYPE[status_code]

    for marker in _TRANSIENT_BODY_MARKERS:
        if marker in msg_lower:
            return ErrorType.TRANSIENT
    return ErrorType.UNKNOWN


__all__ = ["ErrorType", "classify_error"]
