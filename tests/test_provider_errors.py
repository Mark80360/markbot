"""Tests for markbot.providers.errors — ErrorType enum and classify_error."""

from __future__ import annotations

import pytest

from markbot.providers.base import LLMResponse
from markbot.providers.errors import ErrorType, classify_error


class TestClassifyErrorStatusCodes:
    @pytest.mark.parametrize(
        "code",
        [408, 429, 500, 502, 503, 504, 529],
    )
    def test_transient_status_codes(self, code: int) -> None:
        assert classify_error(code, "anything") == ErrorType.TRANSIENT

    @pytest.mark.parametrize(
        "code",
        [401, 402, 403, 404],
    )
    def test_unavailable_status_codes(self, code: int) -> None:
        assert classify_error(code, "anything") == ErrorType.UNAVAILABLE

    def test_unknown_status_code_falls_through_to_message(self) -> None:
        assert classify_error(200, "rate limit exceeded") == ErrorType.TRANSIENT

    def test_unknown_status_and_unknown_message(self) -> None:
        assert classify_error(418, "weird tea pot") == ErrorType.UNKNOWN


class TestClassifyErrorMessages:
    @pytest.mark.parametrize(
        "msg",
        [
            "rate limit exceeded",
            "Rate_Limit hit",
            "too many requests",
            "throttle backoff",
            "connection timeout",
            "timed out waiting",
            "server error 500",
            "internal server error",
            "service unavailable",
            "API overloaded",
            "service at capacity",
            "service is busy",
            "temporarily unavailable",
            "please try again later",
        ],
    )
    def test_transient_messages(self, msg: str) -> None:
        assert classify_error(None, msg) == ErrorType.TRANSIENT

    @pytest.mark.parametrize(
        "msg",
        [
            "401 unauthorized",
            "invalid api key",
            "authentication failed",
            "402 insufficient balance",
            "insufficient_quota",
            "quota exceeded",
            "403 forbidden",
            "access denied",
            "model not found",
            "model_not_found",
            "invalid function arguments",
            "invalid params",
        ],
    )
    def test_unavailable_messages(self, msg: str) -> None:
        assert classify_error(None, msg) == ErrorType.UNAVAILABLE

    @pytest.mark.parametrize(
        "msg",
        ["content_filter triggered", "content filter active"],
    )
    def test_content_messages(self, msg: str) -> None:
        assert classify_error(None, msg) == ErrorType.CONTENT

    def test_unknown_message(self) -> None:
        assert classify_error(None, "weird unrecognised text") == ErrorType.UNKNOWN

    def test_status_code_takes_precedence(self) -> None:
        assert classify_error(401, "rate limit") == ErrorType.UNAVAILABLE


class TestClassifyErrorEdgeCases:
    def test_none_status_with_empty_message(self) -> None:
        assert classify_error(None, "") == ErrorType.UNKNOWN

    def test_none_status_with_none_message(self) -> None:
        assert classify_error(None, None) == ErrorType.UNKNOWN

    def test_message_is_lowercased_before_matching(self) -> None:
        assert classify_error(None, "RATE LIMIT EXCEEDED") == ErrorType.TRANSIENT


class TestErrorTypeEnum:
    def test_values(self) -> None:
        assert ErrorType.TRANSIENT.value == "transient"
        assert ErrorType.UNAVAILABLE.value == "unavailable"
        assert ErrorType.CONTENT.value == "content"
        assert ErrorType.UNKNOWN.value == "unknown"

    def test_subclass_of_str(self) -> None:
        assert isinstance(ErrorType.TRANSIENT, str)


class TestLLMResponseErrorType:
    def test_default_is_none(self) -> None:
        r = LLMResponse(content="hello")
        assert r.error_type is None

    def test_round_trip(self) -> None:
        r = LLMResponse(content="oops", finish_reason="error", error_type=ErrorType.TRANSIENT)
        assert r.error_type == ErrorType.TRANSIENT

    def test_backward_compat_no_argument(self) -> None:
        r = LLMResponse(content="hi", finish_reason="stop", tool_calls=[])
        assert r.error_type is None
        assert r.finish_reason == "stop"
