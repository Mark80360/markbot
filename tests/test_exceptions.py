"""Tests for markbot.types.exceptions — unified exception hierarchy."""

import pytest

from markbot.types.exceptions import (
    AuthenticationError,
    BudgetExceededError,
    ConfigError,
    ConfigValidationError,
    FatalError,
    MarkbotError,
    ModelNotFoundError,
    PermissionDeniedError,
    RateLimitError,
    SecurityError,
    SessionCorruptedError,
    SessionError,
    SessionWriteError,
    SSRFError,
    TimeoutError,
    TransientError,
)


class TestExceptionHierarchy:
    def test_base_exception(self):
        e = MarkbotError("test error")
        assert str(e) == "test error"
        assert e.details == {}
        assert isinstance(e, Exception)

    def test_base_exception_with_details(self):
        e = MarkbotError("test", details={"key": "value"})
        assert e.details == {"key": "value"}

    def test_transient_is_markbot_error(self):
        assert issubclass(TransientError, MarkbotError)

    def test_rate_limit_is_transient(self):
        assert issubclass(RateLimitError, TransientError)

    def test_rate_limit_retry_after(self):
        e = RateLimitError(retry_after_s=30.0, provider="anthropic")
        assert e.retry_after_s == 30.0
        assert e.provider == "anthropic"
        assert e.details["provider"] == "anthropic"

    def test_timeout_is_transient(self):
        assert issubclass(TimeoutError, TransientError)

    def test_fatal_is_markbot_error(self):
        assert issubclass(FatalError, MarkbotError)

    def test_auth_is_fatal(self):
        assert issubclass(AuthenticationError, FatalError)

    def test_auth_provider(self):
        e = AuthenticationError(provider="openai")
        assert e.provider == "openai"

    def test_model_not_found(self):
        e = ModelNotFoundError(model="gpt-5", provider="openai")
        assert e.model == "gpt-5"
        assert e.provider == "openai"
        assert "gpt-5" in e.details["model"]

    def test_config_validation_error(self):
        e = ConfigValidationError(errors=["field A is bad", "field B is missing"])
        assert len(e.errors) == 2
        assert "field A is bad" in e.errors

    def test_session_corrupted(self):
        e = SessionCorruptedError(session_key="cli:direct")
        assert e.session_key == "cli:direct"

    def test_session_write(self):
        e = SessionWriteError(session_key="cli:direct")
        assert e.session_key == "cli:direct"

    def test_budget_exceeded(self):
        e = BudgetExceededError(current_cost=10.0, budget=5.0)
        assert e.current_cost == 10.0
        assert e.budget == 5.0
        assert "10.0" in str(e)

    def test_permission_denied(self):
        e = PermissionDeniedError(tool_name="shell", reason="not allowed")
        assert e.tool_name == "shell"
        assert e.reason == "not allowed"

    def test_ssrf_error(self):
        e = SSRFError(url="http://169.254.169.254/")
        assert e.url == "http://169.254.169.254/"

    def test_catch_all_with_base(self):
        with pytest.raises(MarkbotError):
            raise RateLimitError()

        with pytest.raises(MarkbotError):
            raise AuthenticationError()

        with pytest.raises(MarkbotError):
            raise ConfigValidationError()

        with pytest.raises(MarkbotError):
            raise BudgetExceededError()
