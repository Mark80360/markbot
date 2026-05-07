"""Unified exception hierarchy for markbot.

All markbot-specific exceptions inherit from MarkbotError, enabling
callers to catch the entire family with a single except clause while
still allowing fine-grained handling of specific categories.

Hierarchy:

    MarkbotError
    ├── TransientError          — retryable / temporary failures
    │   ├── RateLimitError      — 429 / 529 throttling
    │   ├── TimeoutError        — request / connection timeouts
    │   └── ServiceUnavailableError — 502/503/504 upstream errors
    ├── FatalError              — non-retryable / permanent failures
    │   ├── AuthenticationError — 401 / 403 credential issues
    │   ├── QuotaExceededError  — 402 billing / quota exhaustion
    │   ├── ModelNotFoundError  — model does not exist
    │   └── InvalidParamsError  — malformed request
    ├── ConfigError             — configuration / setup issues
    │   ├── ConfigValidationError
    │   └── ConfigMigrationError
    ├── SessionError            — session persistence issues
    │   ├── SessionCorruptedError
    │   └── SessionWriteError
    ├── BudgetExceededError     — cost cap exceeded
    ├── PermissionDeniedError   — tool / action not permitted
    └── SecurityError           — security policy violations
        ├── PIIExposureError
        └── SSRFError
"""

from __future__ import annotations


class MarkbotError(Exception):
    """Base for all markbot exceptions."""

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        self.details = details or {}
        super().__init__(message)


class TransientError(MarkbotError):
    """Retryable / temporary failure — caller should back off and retry."""

    retry_after_s: float | None = None

    def __init__(
        self,
        message: str = "",
        *,
        retry_after_s: float | None = None,
        details: dict | None = None,
    ) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(message, details=details)


class RateLimitError(TransientError):
    """429 / 529 — API rate limit or throttling."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        *,
        retry_after_s: float | None = None,
        provider: str = "",
        details: dict | None = None,
    ) -> None:
        _details = {"provider": provider, **(details or {})}
        super().__init__(message, retry_after_s=retry_after_s, details=_details)
        self.provider = provider


class TimeoutError(TransientError):
    """Request or connection timeout."""


class ServiceUnavailableError(TransientError):
    """502/503/504 — upstream server temporarily unavailable."""


class FatalError(MarkbotError):
    """Non-retryable / permanent failure."""


class AuthenticationError(FatalError):
    """401 / 403 — credential or access issues."""

    def __init__(
        self,
        message: str = "Authentication failed",
        *,
        provider: str = "",
        details: dict | None = None,
    ) -> None:
        _details = {"provider": provider, **(details or {})}
        super().__init__(message, details=_details)
        self.provider = provider


class QuotaExceededError(FatalError):
    """402 — billing quota exhausted."""


class ModelNotFoundError(FatalError):
    """Requested model does not exist on the provider."""

    def __init__(
        self,
        message: str = "Model not found",
        *,
        model: str = "",
        provider: str = "",
        details: dict | None = None,
    ) -> None:
        _details = {"model": model, "provider": provider, **(details or {})}
        super().__init__(message, details=_details)
        self.model = model
        self.provider = provider


class InvalidParamsError(FatalError):
    """Malformed request parameters (e.g. invalid tool arguments)."""


class ConfigError(MarkbotError):
    """Configuration / setup issues."""


class ConfigValidationError(ConfigError):
    """Configuration failed validation checks."""

    def __init__(
        self,
        message: str = "Configuration validation failed",
        *,
        errors: list[str] | None = None,
        details: dict | None = None,
    ) -> None:
        _details = {"errors": errors or [], **(details or {})}
        super().__init__(message, details=_details)
        self.errors = errors or []


class ConfigMigrationError(ConfigError):
    """Configuration schema migration failed."""

    def __init__(
        self,
        message: str = "Configuration migration failed",
        *,
        from_version: int = 0,
        to_version: int = 0,
        details: dict | None = None,
    ) -> None:
        _details = {"from_version": from_version, "to_version": to_version, **(details or {})}
        super().__init__(message, details=_details)
        self.from_version = from_version
        self.to_version = to_version


class SessionError(MarkbotError):
    """Session persistence issues."""


class SessionCorruptedError(SessionError):
    """Session data is corrupted and cannot be loaded."""

    def __init__(
        self,
        message: str = "Session data corrupted",
        *,
        session_key: str = "",
        details: dict | None = None,
    ) -> None:
        _details = {"session_key": session_key, **(details or {})}
        super().__init__(message, details=_details)
        self.session_key = session_key


class SessionWriteError(SessionError):
    """Failed to persist session data to disk."""

    def __init__(
        self,
        message: str = "Session write failed",
        *,
        session_key: str = "",
        details: dict | None = None,
    ) -> None:
        _details = {"session_key": session_key, **(details or {})}
        super().__init__(message, details=_details)
        self.session_key = session_key


class BudgetExceededError(MarkbotError):
    """Cost cap exceeded."""

    def __init__(
        self,
        current_cost: float = 0.0,
        budget: float = 0.0,
        *,
        details: dict | None = None,
    ) -> None:
        self.current_cost = current_cost
        self.budget = budget
        _details = {"current_cost": current_cost, "budget": budget, **(details or {})}
        super().__init__(
            f"Budget exceeded: ${current_cost:.6f} > ${budget:.6f}",
            details=_details,
        )


class PermissionDeniedError(MarkbotError):
    """Tool or action not permitted."""

    def __init__(
        self,
        message: str = "Permission denied",
        *,
        tool_name: str = "",
        reason: str = "",
        details: dict | None = None,
    ) -> None:
        _details = {"tool_name": tool_name, "reason": reason, **(details or {})}
        super().__init__(message, details=_details)
        self.tool_name = tool_name
        self.reason = reason


class SecurityError(MarkbotError):
    """Security policy violations."""


class PIIExposureError(SecurityError):
    """PII data detected in output that should be filtered."""


class SSRFError(SecurityError):
    """Server-Side Request Forgery attempt detected."""

    def __init__(
        self,
        message: str = "SSRF attempt blocked",
        *,
        url: str = "",
        details: dict | None = None,
    ) -> None:
        _details = {"url": url, **(details or {})}
        super().__init__(message, details=_details)
        self.url = url
