"""Unified machine-readable error taxonomy.

Every failure surfaced to the model, a channel, or telemetry carries a
stable ``ErrorCode`` so downstream logic (reactive compaction, circuit
breaking, budget accounting, audit) can dispatch on codes instead of
parsing prose.  Model-facing text embeds the code as a ``[CODE]``
prefix; :func:`extract_error_code` recovers it post-hoc from plain
result strings produced before this module existed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ErrorCode(str, Enum):
    """Stable machine codes. Never rename a value — only add."""

    # -- Tool domain --------------------------------------------------
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_SOFT_DISABLED = "TOOL_SOFT_DISABLED"
    TOOL_PERMISSION_DENIED = "TOOL_PERMISSION_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    APPROVAL_UNAVAILABLE = "APPROVAL_UNAVAILABLE"
    APPROVAL_FAILED = "APPROVAL_FAILED"
    TOOL_VALIDATION_FAILED = "TOOL_VALIDATION_FAILED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    LOOP_CAP_EXCEEDED = "LOOP_CAP_EXCEEDED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_CANCELLED_BEFORE_DISPATCH = "TOOL_CANCELLED_BEFORE_DISPATCH"
    TOOL_CANCELLED = "TOOL_CANCELLED"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"

    # -- LLM domain ---------------------------------------------------
    CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    PROVIDER_OVERLOADED = "PROVIDER_OVERLOADED"
    PROVIDER_SERVER_ERROR = "PROVIDER_SERVER_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_TRANSPORT_ERROR = "PROVIDER_TRANSPORT_ERROR"
    INVALID_CREDENTIAL = "INVALID_CREDENTIAL"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    ALL_MODELS_FAILED = "ALL_MODELS_FAILED"

    # -- Generic -------------------------------------------------------
    INTERNAL_ERROR = "INTERNAL_ERROR"


_CODE_PATTERN = re.compile(r"\[([A-Z][A-Z0-9_]+)\]")


def extract_error_code(text: Any) -> Optional[ErrorCode]:
    """Recover an :class:`ErrorCode` embedded as ``[CODE]`` in *text*.

    Returns None when *text* carries no recognised code.
    """
    if not isinstance(text, str):
        return None
    match = _CODE_PATTERN.match(text.lstrip())
    if not match:
        return None
    try:
        return ErrorCode(match.group(1))
    except ValueError:
        return None


@dataclass
class ErrorDetail:
    """Structured snapshot carried by :class:`MarkbotError`."""

    retry_after_ms: Optional[int] = None
    status: Optional[int] = None
    request_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in (
            ("retry_after_ms", self.retry_after_ms),
            ("status", self.status),
            ("request_id", self.request_id),
        ):
            if value is not None:
                out[key] = value
        if self.extra:
            out.update(self.extra)
        return out


class MarkbotError(Exception):
    """Base error carrying a stable machine code and retryability.

    Rendering rules:

    * ``str(err)`` returns the bare human message (back-compat).
    * :meth:`model_text` returns ``"[CODE] message"`` — the form tools
      return into the conversation so both the model and post-hoc
      classifiers can see the code.
    """

    default_code: ErrorCode = ErrorCode.INTERNAL_ERROR
    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        retryable: bool | None = None,
        detail: ErrorDetail | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        if cause is not None:
            # Chain without raising-from syntax so wrapping inside except
            # blocks keeps the original traceback reachable.
            self.__cause__ = cause
        self.message = message
        self.code = code or self.default_code
        self.retryable = self.default_retryable if retryable is None else retryable
        self.detail = detail or ErrorDetail()

    def model_text(self) -> str:
        return f"[{self.code.value}] {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            **self.detail.to_dict(),
        }

    def __str__(self) -> str:  # keep logs readable
        return self.message


# ---------------------------------------------------------------------------
# Tool-domain errors
# ---------------------------------------------------------------------------


class ToolError(MarkbotError):
    default_code = ErrorCode.TOOL_EXECUTION_FAILED


class ToolNotFoundError(ToolError):
    default_code = ErrorCode.TOOL_NOT_FOUND


class ToolSoftDisabledError(ToolError):
    default_code = ErrorCode.TOOL_SOFT_DISABLED


class ToolPermissionDeniedError(ToolError):
    default_code = ErrorCode.TOOL_PERMISSION_DENIED


class ApprovalRequiredError(ToolError):
    """Raised when a tool needs interactive approval that must be asked."""

    default_code = ErrorCode.APPROVAL_REQUIRED


class ApprovalDeniedError(ToolError):
    default_code = ErrorCode.APPROVAL_DENIED


class ApprovalUnavailableError(ToolError):
    default_code = ErrorCode.APPROVAL_UNAVAILABLE


class ApprovalFailedError(ToolError):
    default_code = ErrorCode.APPROVAL_FAILED


class ToolValidationError(ToolError):
    default_code = ErrorCode.TOOL_VALIDATION_FAILED


class GuardrailBlockedError(ToolError):
    default_code = ErrorCode.GUARDRAIL_BLOCKED


class LoopCapExceededError(ToolError):
    default_code = ErrorCode.LOOP_CAP_EXCEEDED


class ToolTimeoutError(ToolError):
    default_code = ErrorCode.TOOL_TIMEOUT

    def __init__(
        self,
        message: str,
        *,
        timeout_ms: int | None = None,
        **kwargs: Any,
    ) -> None:
        detail = kwargs.pop("detail", None) or ErrorDetail()
        if timeout_ms is not None:
            detail.extra["timeout_ms"] = timeout_ms
        super().__init__(message, detail=detail, **kwargs)


class ToolCancelledBeforeDispatchError(ToolError):
    default_code = ErrorCode.TOOL_CANCELLED_BEFORE_DISPATCH


class ToolCancelledError(ToolError):
    default_code = ErrorCode.TOOL_CANCELLED


# ---------------------------------------------------------------------------
# LLM-domain errors
# ---------------------------------------------------------------------------


class LLMError(MarkbotError):
    default_code = ErrorCode.PROVIDER_SERVER_ERROR
    default_retryable = True


class ContextWindowExceededError(LLMError):
    default_code = ErrorCode.CONTEXT_WINDOW_EXCEEDED
    default_retryable = False


class RateLimitedError(LLMError):
    default_code = ErrorCode.RATE_LIMITED


class QuotaExceededError(LLMError):
    default_code = ErrorCode.QUOTA_EXCEEDED
    default_retryable = False


class ProviderOverloadedError(LLMError):
    default_code = ErrorCode.PROVIDER_OVERLOADED


class ProviderTimeoutError(LLMError):
    default_code = ErrorCode.PROVIDER_TIMEOUT


class ProviderTransportError(LLMError):
    default_code = ErrorCode.PROVIDER_TRANSPORT_ERROR


class InvalidCredentialError(LLMError):
    default_code = ErrorCode.INVALID_CREDENTIAL
    default_retryable = False


class EmptyResponseError(LLMError):
    default_code = ErrorCode.EMPTY_RESPONSE


#: Substrings (lowercased) mapped to typed LLM errors. Provider SDKs raise
#: heterogeneous exceptions; until every provider wraps its failures, string
#: classification at the boundary remains the single funnel.
_LLM_STRING_PATTERNS: tuple[tuple[tuple[str, ...], type[MarkbotError]], ...] = (
    (
        ("prompt too long", "context length", "maximum context", "context window",
         "too many tokens", "too large for the model", "exceed_context",
         "exceeds the available context size", "available context size"),
        ContextWindowExceededError,
    ),
    (("rate limit", "ratelimit", "429", "too many requests"), RateLimitedError),
    (("quota", "billing", "insufficient"), QuotaExceededError),
    (("invalid api key", "unauthorized", "401", "authentication", "api key not valid"),
     InvalidCredentialError),
    (("overloaded", "503", "capacity"), ProviderOverloadedError),
    (("timeout", "timed out"), ProviderTimeoutError),
    (("connection", "eof occurred", "network", "reset by peer", "502", "bad gateway"),
     ProviderTransportError),
)


def classify_exception(exc: BaseException) -> MarkbotError:
    """Return *exc* itself when already typed, else wrap via string patterns.

    Unknown exceptions fall back to :class:`LLMError` when they look
    provider-ish, otherwise :class:`MarkbotError` INTERNAL. The original
    exception is preserved as ``__cause__`` on wrap.
    """
    if isinstance(exc, MarkbotError):
        return exc
    text = str(exc).lower()
    for needles, cls in _LLM_STRING_PATTERNS:
        if any(needle in text for needle in needles):
            return cls(str(exc), cause=exc)  # type: ignore[call-arg]
    return LLMError(
        str(exc), code=ErrorCode.INTERNAL_ERROR, retryable=False, cause=exc
    )


def is_prompt_too_long(exc: BaseException) -> bool:
    """True when *exc* classifies as context-window overflow."""
    classified = classify_exception(exc)
    return isinstance(classified, ContextWindowExceededError) or (
        isinstance(exc, MarkbotError) and exc.code == ErrorCode.CONTEXT_WINDOW_EXCEEDED
    )


def tool_result_error(code: ErrorCode, message: str, **extra: Any) -> str:
    """Canonical model-facing error result with machine-readable prefix."""
    suffix = ""
    if extra:
        pairs = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None)
        if pairs:
            suffix = f" ({pairs})"
    return f"[{code.value}] {message}{suffix}"
