"""Tests for the unified machine-readable error taxonomy."""

import pytest

from markbot.errors import (
    ContextWindowExceededError,
    ErrorCode,
    ErrorDetail,
    InvalidCredentialError,
    MarkbotError,
    RateLimitedError,
    ToolTimeoutError,
    classify_exception,
    extract_error_code,
    is_prompt_too_long,
    tool_result_error,
)


class TestErrorCode:
    def test_codes_are_stable_strings(self):
        assert ErrorCode.TOOL_NOT_FOUND.value == "TOOL_NOT_FOUND"
        assert ErrorCode.CONTEXT_WINDOW_EXCEEDED.value == "CONTEXT_WINDOW_EXCEEDED"

    def test_extract_from_prefixed_text(self):
        text = tool_result_error(ErrorCode.TOOL_NOT_FOUND, "no such tool")
        assert extract_error_code(text) == ErrorCode.TOOL_NOT_FOUND

    def test_extract_returns_none_for_plain_text(self):
        assert extract_error_code("Error: something broke") is None
        assert extract_error_code(None) is None
        assert extract_error_code({"a": 1}) is None

    def test_extract_ignores_unknown_codes(self):
        assert extract_error_code("[NOT_A_REAL_CODE] hi") is None


class TestMarkbotError:
    def test_defaults(self):
        err = MarkbotError("boom")
        assert err.code == ErrorCode.INTERNAL_ERROR
        assert err.retryable is False
        assert str(err) == "boom"

    def test_model_text_embeds_code(self):
        err = RateLimitedError("slow down", detail=ErrorDetail(retry_after_ms=2000))
        assert err.model_text().startswith("[RATE_LIMITED] slow down")
        assert err.detail.retry_after_ms == 2000

    def test_retryable_flag(self):
        assert RateLimitedError("x").retryable is True
        assert ContextWindowExceededError("x").retryable is False

    def test_to_dict(self):
        err = ToolTimeoutError("t", timeout_ms=1234)
        d = err.to_dict()
        assert d["code"] == "TOOL_TIMEOUT"
        assert d["timeout_ms"] == 1234


class TestClassifyException:
    def test_passthrough_typed(self):
        original = ContextWindowExceededError("already typed")
        assert classify_exception(original) is original

    def test_string_patterns(self):
        cases = [
            ("prompt too long for model", ContextWindowExceededError),
            ("maximum context length exceeded", ContextWindowExceededError),
            ("HTTP 429 too many requests", RateLimitedError),
            ("invalid api key provided", InvalidCredentialError),
        ]
        for message, cls in cases:
            classified = classify_exception(RuntimeError(message))
            assert isinstance(classified, cls), message

    def test_wraps_unknown_as_internal(self):
        classified = classify_exception(ValueError("weird"))
        assert classified.code == ErrorCode.INTERNAL_ERROR
        assert classified.retryable is False
        # original preserved as cause
        assert isinstance(classified.__cause__, ValueError)

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("This model's maximum context length is 4097 tokens"),
            RuntimeError("prompt too long"),
        ],
    )
    def test_is_prompt_too_long_true(self, exc):
        assert is_prompt_too_long(exc) is True

    def test_is_prompt_too_long_false(self):
        assert is_prompt_too_long(RuntimeError("connection reset")) is False


class TestBackwardCompatAliases:
    def test_all_models_failed_is_typed(self):
        from markbot.providers import AllModelsFailedError

        err = AllModelsFailedError("chain exhausted")
        assert err.code == ErrorCode.ALL_MODELS_FAILED
        assert isinstance(err, MarkbotError)
        assert err.attempts == []
        assert err.last_error is None
