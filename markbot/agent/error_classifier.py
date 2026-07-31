"""API error classifier for smart retry / fallback / recovery decisions.

Classifies API errors with structured recovery action hints:

  - **Deterministic** (retryable=False): SSL cert, 400 format, 401/403 auth,
    404 model not found, 413 payload too large, context overflow, content
    policy, billing exhaustion.

  - **Transient** (retryable=True): 408 timeout, 429 rate limit, 500/502/503/
    504/529 server error / overloaded, connection reset / refused.

Each classification carries recovery hints:
  - ``should_compress`` — context overflow → trigger compact before retry
  - ``should_fallback`` — model/provider problem → try next in fallback chain
  - ``should_strip_images`` — image/payload issue → retry without images
  - ``backoff_seconds`` / ``backoff_strategy`` — suggested retry delay

Wired into the retry loop in providers/base.py via ``classify_to_error_type``
(bridge to ``ErrorType``) and directly via ``classify_api_error`` for
recovery-action decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


# ── Error category ─────────────────────────────────────────────────────

class ErrorCategory(str, Enum):
    """Two-way classification for retry decisions."""

    DETERMINISTIC = "deterministic"
    """Non-retryable: retrying reproduces the same failure. Fail or recover."""

    TRANSIENT = "transient"
    """Retryable: timeout, rate limit, server error. Retry with backoff."""


class BackoffStrategy(str, Enum):
    """Backoff strategy hint for the retry loop."""

    FIXED = "fixed"
    """Constant delay between retries."""
    EXPONENTIAL = "exponential"
    """Delay doubles each attempt: base, base*2, base*4, ..."""
    JITTERED = "jittered"
    """Exponential with ±25% jitter to avoid thundering herd."""


@dataclass(frozen=True)
class ClassifiedError:
    """Structured result of classifying an API error.

    Recovery action hints let the retry loop go beyond simple retry/fail:
      - ``should_compress``: trigger context compression (compact) then retry
      - ``should_fallback``: skip to next model/provider in the fallback chain
      - ``should_strip_images``: remove image content then retry
      - ``backoff_seconds`` / ``backoff_strategy``: how long to wait
    """

    category: ErrorCategory
    reason: str
    retryable: bool
    message: str = ""
    status_code: int | None = None
    # Recovery action hints
    should_compress: bool = False
    should_fallback: bool = False
    should_strip_images: bool = False
    # Backoff hints (only meaningful when retryable=True)
    backoff_seconds: float = 0.0
    backoff_strategy: BackoffStrategy = BackoffStrategy.FIXED

    @property
    def is_deterministic(self) -> bool:
        return self.category is ErrorCategory.DETERMINISTIC


# ── Pattern tables ──────────────────────────────────────────────────────

_SSL_CERT_RE = re.compile(
    r"ssl[\s_]?cert|certificate[\s_]?verif|sslv?error|"
    r"cert[\s_]?verif|unable[\s_]?to[\s_]?get[\s_]?local[\s_]?issuer|"
    r"self[\s_-]?signed|bad[\s_]?handshake",
    re.IGNORECASE,
)

_CONTEXT_OVERFLOW_RE = re.compile(
    r"context[\s_]?length|context[\s_]?size|maximum[\s_]?context|"
    r"token[\s_]?limit|too[\s_]?many[\s_]?tokens|context[\s_]?window|"
    r"reduce[\s_]?the[\s_]?length|maximum[\s_]?number[\s_]?of[\s_]?tokens|"
    r"prompt[\s_]?is[\s_]?too[\s_]?long|prompt[\s_]?exceeds|"
    r"max_model_len|exceeds[\s_]?the[\s_]?max|input[\s_]?is[\s_]?too[\s_]?long|"
    r"maximum[\s_]?allowed[\s_]?input[\s_]?length|"
    r"超过最大长度|上下文长度",
    re.IGNORECASE,
)

_TIMEOUT_RE = re.compile(
    r"timeout|timed[\s_]?out|connection[\s_]?timeout|read[\s_]?timeout",
    re.IGNORECASE,
)

# Image-too-large patterns (400 with image-specific message, not 413).
_IMAGE_TOO_LARGE_RE = re.compile(
    r"image[\s_]?exceeds|image[\s_]?too[\s_]?large|image_too_large|"
    r"image[\s_]?size[\s_]?exceeds|image[\s_]?dimensions[\s_]?exceeds|"
    r"dimensions[\s_]?exceed[\s_]?max",
    re.IGNORECASE,
)

# Multimodal tool content incompatibility (provider rejects list-type tool
# message content — strip image parts and retry as text).
_MULTIMODAL_TOOL_CONTENT_RE = re.compile(
    r"text[\s_]?is[\s_]?not[\s_]?set|"
    r"tool[\s_]?message[\s_]?content[\s_]?must[\s_]?be[\s_]?a[\s_]?string|"
    r"tool[\s_]?content[\s_]?must[\s_]?be[\s_]?a[\s_]?string|"
    r"tool[\s_]?message[\s_]?must[\s_]?be[\s_]?a[\s_]?string|"
    r"expected[\s_]?string,[\s_]?got[\s_]?(?:list|array)|"
    r"tool_call\.content[\s_]?must[\s_]?be[\s_]?string",
    re.IGNORECASE,
)

# Payload-too-large patterns (413 or embedded in message).
_PAYLOAD_TOO_LARGE_RE = re.compile(
    r"request[\s_]?entity[\s_]?too[\s_]?large|payload[\s_]?too[\s_]?large|"
    r"error[\s_]?code:[\s_]?413|request_too_large|"
    r"request[\s_]?exceeds[\s_]?the[\s_]?maximum[\s_]?size",
    re.IGNORECASE,
)

# Model not found patterns (body-level, overrides 5xx from aggregators).
_MODEL_NOT_FOUND_RE = re.compile(
    r"is[\s_]?not[\s_]?a[\s_]?valid[\s_]?model|invalid[\s_]?model|"
    r"model[\s_]?not[\s_]?found|model_not_found|does[\s_]?not[\s_]?exist|"
    r"no[\s_]?such[\s_]?model|unknown[\s_]?model|unsupported[\s_]?model|"
    r"no[\s_]?endpoints[\s_]?found",
    re.IGNORECASE,
)

# Billing patterns (deterministic — rotate credential or top up).
_BILLING_RE = re.compile(
    r"insufficient[\s_]?credits|insufficient_quota|insufficient[\s_]?balance|"
    r"credits?[\s_]?exhaust|no[\s_]?usable[\s_]?credits|top[\s_]?up[\s_]?your|"
    r"payment[\s_]?required|billing[\s_]?hard[\s_]?limit|"
    r"exceeded[\s_]?your[\s_]?current[\s_]?quota|account[\s_]?is[\s_]?deactivated|"
    r"plan[\s_]?does[\s_]?not[\s_]?include|out[\s_]?of[\s_]?(?:extra[\s_]?usage|funds)|"
    r"balance_depleted|not[\s_]?available[\s_]?on[\s_]?the[\s_]?free[\s_]?tier",
    re.IGNORECASE,
)

# Auth patterns (deterministic — rotate credential).
_AUTH_RE = re.compile(
    r"unauthorized|invalid[\s_]?api[\s_]?key|authentication[\s_]?failed|"
    r"forbidden|access[\s_]?denied|invalid[\s_]?token",
    re.IGNORECASE,
)

# Overloaded patterns (transient — server is busy, NOT rate limit).
# Matched BEFORE generic rate-limit patterns so "overloaded" doesn't
# mis-route to the rate_limit bucket with its shorter backoff.
_OVERLOADED_RE = re.compile(
    r"overloaded|temporarily[\s_]?overloaded|service[\s_]?is[\s_]?temporarily|"
    r"server[\s_]?is[\s_]?overloaded|service[\s_]?overloaded|"
    r"at[\s_]?capacity|over[\s_]?capacity",
    re.IGNORECASE,
)

# Rate limit patterns (transient — per-key throttling, backoff then retry).
_RATE_LIMIT_RE = re.compile(
    r"rate[\s_]?limit|rate_limit|too[\s_]?many[\s_]?requests|throttl|"
    r"requests[\s_]?per[\s_]?(?:minute|day)|tokens[\s_]?per[\s_]?minute|"
    r"try[\s_]?again[\s_]?in|please[\s_]?retry[\s_]?after|resource_exhausted|"
    r"too[\s_]?many[\s_]?concurrent",
    re.IGNORECASE,
)

# Content policy / safety filter (deterministic — don't retry unchanged).
_CONTENT_POLICY_RE = re.compile(
    r"content_filter|content[\s_]?policy|safety[\s_]?filter|"
    r"content[\s_]?management[\s_]?policy|triggered[\s_]?safety",
    re.IGNORECASE,
)

# Transient connection-level markers (no status code available).
_TRANSIENT_CONNECTION_RE = re.compile(
    r"connection[\s_]?reset|connection[\s_]?refused|connection[\s_]?aborted|"
    r"connection[\s_]?closed|remote[\s_]?end[\s_]?closed|"
    r"server[\s_]?error|internal[\s_]?server[\s_]?error|"
    r"service[\s_]?unavailable|temporarily[\s_]?unavailable|try[\s_]?again",
    re.IGNORECASE,
)

# No available channel (aggregator-specific: all upstream channels dead).
_NO_CHANNEL_RE = re.compile(
    r"no[\s_]?available[\s_]?channel|all[\s_]?channels[\s_]?(?:failed|exhausted)",
    re.IGNORECASE,
)

# Format error patterns (deterministic 400 — bad request shape).
_FORMAT_ERROR_RE = re.compile(
    r"invalid[\s_]?function[\s_]?arguments|invalid[\s_]?params|"
    r"bad[\s_]?request|invalid[\s_]?request[\s_]?body|"
    r"all[\s_]?messages[\s_]?must[\s_]?have[\s_]?non-empty",
    re.IGNORECASE,
)


# ── HTTP status → (category, reason, hints) ────────────────────────────

@dataclass(frozen=True)
class _StatusHint:
    category: ErrorCategory
    reason: str
    retryable: bool
    should_compress: bool = False
    should_fallback: bool = False
    should_strip_images: bool = False
    backoff_seconds: float = 0.0
    backoff_strategy: BackoffStrategy = BackoffStrategy.FIXED


_STATUS_MAP: dict[int, _StatusHint] = {
    400: _StatusHint(ErrorCategory.DETERMINISTIC, "format_error", False),
    401: _StatusHint(ErrorCategory.DETERMINISTIC, "auth_error", False, should_fallback=True),
    402: _StatusHint(ErrorCategory.DETERMINISTIC, "billing_error", False, should_fallback=True),
    403: _StatusHint(ErrorCategory.DETERMINISTIC, "auth_error", False, should_fallback=True),
    404: _StatusHint(ErrorCategory.DETERMINISTIC, "model_not_found", False, should_fallback=True),
    408: _StatusHint(ErrorCategory.TRANSIENT, "timeout", True, backoff_seconds=1.0),
    413: _StatusHint(ErrorCategory.DETERMINISTIC, "payload_too_large", False, should_compress=True, should_strip_images=True),
    429: _StatusHint(ErrorCategory.TRANSIENT, "rate_limit", True, backoff_seconds=5.0, backoff_strategy=BackoffStrategy.JITTERED),
    432: _StatusHint(ErrorCategory.DETERMINISTIC, "billing", False, should_fallback=True),
    500: _StatusHint(ErrorCategory.TRANSIENT, "server_error", True, backoff_seconds=1.0),
    502: _StatusHint(ErrorCategory.TRANSIENT, "bad_gateway", True, backoff_seconds=1.0),
    503: _StatusHint(ErrorCategory.TRANSIENT, "service_unavailable", True, backoff_seconds=3.0, backoff_strategy=BackoffStrategy.EXPONENTIAL),
    504: _StatusHint(ErrorCategory.TRANSIENT, "gateway_timeout", True, backoff_seconds=2.0),
    529: _StatusHint(ErrorCategory.TRANSIENT, "overloaded", True, backoff_seconds=3.0, backoff_strategy=BackoffStrategy.EXPONENTIAL),
}


# ── Classification ─────────────────────────────────────────────────────

def classify_api_error(
    status_code: int | None = None,
    message: str = "",
    exception: BaseException | None = None,
) -> ClassifiedError:
    """Classify an API error with recovery action hints.

    Resolution order (most-specific first):
      1.  SSL certificate verification (deterministic — fail fast)
      2.  Context overflow (deterministic — should_compress)
      3.  Content policy / safety filter (deterministic — should_fallback)
      4.  Image too large (deterministic — should_strip_images)
      5.  Multimodal tool content incompatible (deterministic — should_strip_images)
      6.  Payload too large (deterministic — should_compress)
      7.  Billing exhaustion (deterministic — should_fallback)
      8.  Auth failure (deterministic — should_fallback)
      9.  Model not found (deterministic — should_fallback)
     10.  No available channel (deterministic — should_fallback)
     11.  Format error (deterministic — 400 body markers)
     12.  HTTP status code (with body-aware refinement for 429/5xx)
     13.  Overloaded (transient — body marker, longer backoff)
     14.  Rate limit (transient — body marker, jittered backoff)
     15.  Timeout (transient — body marker)
     16.  Transient connection (transient — body marker)
     17.  Unknown → transient (safest default — retry with backoff)
    """
    msg = message or (repr(exception) if exception else "")
    msg_lower = msg.lower()

    def _result(
        reason: str,
        category: ErrorCategory,
        *,
        retryable: bool | None = None,
        should_compress: bool = False,
        should_fallback: bool = False,
        should_strip_images: bool = False,
        backoff_seconds: float = 0.0,
        backoff_strategy: BackoffStrategy = BackoffStrategy.FIXED,
    ) -> ClassifiedError:
        return ClassifiedError(
            category=category,
            reason=reason,
            retryable=retryable if retryable is not None else (category is ErrorCategory.TRANSIENT),
            message=msg,
            status_code=status_code,
            should_compress=should_compress,
            should_fallback=should_fallback,
            should_strip_images=should_strip_images,
            backoff_seconds=backoff_seconds,
            backoff_strategy=backoff_strategy,
        )

    # 1. SSL certificate verification — deterministic, fail fast
    if _SSL_CERT_RE.search(msg):
        return _result("ssl_cert_verification", ErrorCategory.DETERMINISTIC, should_fallback=True)

    # 2. Context overflow — deterministic, compress before retry.
    # Also set should_strip_images as a fallback recovery: if compact is not
    # wired into the retry loop yet, stripping images can still reduce tokens.
    if _CONTEXT_OVERFLOW_RE.search(msg):
        return _result(
            "context_overflow", ErrorCategory.DETERMINISTIC,
            should_compress=True, should_strip_images=True,
        )

    # 3. Content policy / safety filter — deterministic, try different model
    if _CONTENT_POLICY_RE.search(msg):
        return _result("content_policy", ErrorCategory.DETERMINISTIC, should_fallback=True)

    # 4. Image too large — deterministic, strip images and retry
    if _IMAGE_TOO_LARGE_RE.search(msg):
        return _result("image_too_large", ErrorCategory.DETERMINISTIC, should_strip_images=True)

    # 5. Multimodal tool content incompatible — deterministic, strip images
    if _MULTIMODAL_TOOL_CONTENT_RE.search(msg):
        return _result("multimodal_tool_content", ErrorCategory.DETERMINISTIC, should_strip_images=True)

    # 6. Payload too large (413 body or embedded in message) — compress.
    # Also strip images as fallback (images are the largest payload component).
    if _PAYLOAD_TOO_LARGE_RE.search(msg):
        return _result(
            "payload_too_large", ErrorCategory.DETERMINISTIC,
            should_compress=True, should_strip_images=True,
        )

    # 7. Billing exhaustion — deterministic, rotate credential / fallback
    if _BILLING_RE.search(msg):
        return _result("billing", ErrorCategory.DETERMINISTIC, should_fallback=True)

    # 8. Auth failure — deterministic, rotate credential / fallback
    if _AUTH_RE.search(msg):
        return _result("auth", ErrorCategory.DETERMINISTIC, should_fallback=True)

    # 9. Model not found — deterministic, fallback to different model
    if _MODEL_NOT_FOUND_RE.search(msg):
        return _result("model_not_found", ErrorCategory.DETERMINISTIC, should_fallback=True)

    # 10. No available channel (aggregator-specific) — deterministic, fallback
    if _NO_CHANNEL_RE.search(msg):
        return _result("no_available_channel", ErrorCategory.DETERMINISTIC, should_fallback=True)

    # 11. Format error (400 body markers) — deterministic
    if _FORMAT_ERROR_RE.search(msg):
        return _result("format_error", ErrorCategory.DETERMINISTIC)

    # 12. HTTP status code (with body-aware refinement)
    if status_code is not None and status_code in _STATUS_MAP:
        hint = _STATUS_MAP[status_code]
        # Refine 429: check if body says "overloaded" (server-wide, not per-key)
        if status_code == 429 and _OVERLOADED_RE.search(msg):
            return _result(
                "overloaded", ErrorCategory.TRANSIENT,
                backoff_seconds=3.0, backoff_strategy=BackoffStrategy.EXPONENTIAL,
            )
        # Refine 503: check if body says "no available channel" (permanent)
        if status_code in (502, 503) and _NO_CHANNEL_RE.search(msg):
            return _result(
                "no_available_channel", ErrorCategory.DETERMINISTIC,
                should_fallback=True,
            )
        return _result(
            hint.reason, hint.category,
            should_fallback=hint.should_fallback,
            should_compress=hint.should_compress,
            backoff_seconds=hint.backoff_seconds,
            backoff_strategy=hint.backoff_strategy,
        )

    # 13. Overloaded (transient — body marker, no status code)
    if _OVERLOADED_RE.search(msg):
        return _result(
            "overloaded", ErrorCategory.TRANSIENT,
            backoff_seconds=3.0, backoff_strategy=BackoffStrategy.EXPONENTIAL,
        )

    # 14. Rate limit (transient — body marker, no status code)
    if _RATE_LIMIT_RE.search(msg):
        return _result(
            "rate_limit", ErrorCategory.TRANSIENT,
            backoff_seconds=5.0, backoff_strategy=BackoffStrategy.JITTERED,
        )

    # 15. Timeout (transient — body marker, no status code)
    if _TIMEOUT_RE.search(msg):
        return _result("timeout", ErrorCategory.TRANSIENT, backoff_seconds=1.0)

    # 16. Transient connection (transient — body marker, no status code)
    if _TRANSIENT_CONNECTION_RE.search(msg):
        return _result("transient_error", ErrorCategory.TRANSIENT, backoff_seconds=2.0)

    # 17. Unknown → transient (safest default — retry with backoff)
    return _result(
        "unknown", ErrorCategory.TRANSIENT,
        backoff_seconds=2.0, backoff_strategy=BackoffStrategy.FIXED,
    )


def classify_to_error_type(
    status_code: int | None = None,
    message: str = "",
    exception: BaseException | None = None,
) -> Any:
    """Bridge: classify_api_error → ErrorType for backward compatibility.

    Maps the richer ``ClassifiedError`` to the existing ``ErrorType`` enum
    so providers can switch to the new classifier without changing their
    ``LLMResponse.error_type`` consumers.
    """
    from markbot.providers.errors import ErrorType

    classification = classify_api_error(status_code, message, exception)
    if classification.reason == "content_policy":
        return ErrorType.CONTENT
    if classification.retryable:
        return ErrorType.TRANSIENT
    return ErrorType.UNAVAILABLE


__all__ = [
    "ErrorCategory",
    "BackoffStrategy",
    "ClassifiedError",
    "classify_api_error",
    "classify_to_error_type",
]
