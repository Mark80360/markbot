# P1-4: Typed Error Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ad-hoc substring matching against `_TRANSIENT_ERROR_MARKERS` and `MODEL_UNAVAILABLE_ERRORS` with a structured `LLMResponse.error_type` field that providers fill in at the point of error construction.

**Architecture:** New `markbot/providers/errors.py` defines a 4-value `ErrorType` enum and a `classify_error(status_code, message)` function. Each of the 13 error-return sites in 5 provider files fills in `error_type` via `classify_error`. The 3 call sites in `base.py` and `fallback.py` then drop their substring-matching helpers and use the field directly.

**Tech Stack:** Python 3.13 stdlib (`enum.Enum`), `httpx` (existing), pytest (existing)

**Reference spec:** `docs/superpowers/specs/2026-06-02-p1-4-typed-errors-design.md`

---

## Task 1: Create `errors.py` module with `ErrorType` enum + `classify_error`

**Files:**
- Create: `markbot/providers/errors.py`
- Create: `tests/test_provider_errors.py`

- [ ] **Step 1.1: Write failing test for `classify_error`**

Create `tests/test_provider_errors.py`:

```python
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
        # 200 is not in the map; falls through to message match
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
        # 401 (UNAVAILABLE) wins over "rate limit" (TRANSIENT) message hint
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
        # Existing code that constructs LLMResponse without error_type must still work
        r = LLMResponse(content="hi", finish_reason="stop", tool_calls=[])
        assert r.error_type is None
        assert r.finish_reason == "stop"
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `cd D:\Source\markbot; python -m pytest tests/test_provider_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'markbot.providers.errors'`

- [ ] **Step 1.3: Add `error_type` field to `LLMResponse`**

In `markbot/providers/base.py`, add the import at the top (after existing `from __future__ import annotations`):

```python
from markbot.providers.errors import ErrorType
```

And modify the `LLMResponse` dataclass (around line 42-55) by adding one new field as the last line of fields:

```python
@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1 etc.
    thinking_blocks: list[dict] | None = None  # Anthropic extended thinking
    error_type: ErrorType | None = None  # set when finish_reason in {"error", "content_filter"}
```

- [ ] **Step 1.4: Create `markbot/providers/errors.py`**

```python
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
```

- [ ] **Step 1.5: Run test to verify it passes**

Run: `cd D:\Source\markbot; python -m pytest tests/test_provider_errors.py -v`
Expected: All tests PASS

- [ ] **Step 1.6: Verify full suite still passes (regression check)**

Run: `cd D:\Source\markbot; python -m pytest --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default -q 2>&1 | tail -3`
Expected: All previously-passing tests still pass (no regressions from new LLMResponse field)

- [ ] **Step 1.7: Commit**

```bash
cd D:\Source\markbot; git add markbot/providers/errors.py markbot/providers/base.py tests/test_provider_errors.py; git commit -m "feat(providers): add ErrorType enum and classify_error"
```

---

## Task 2: Migrate `azure_openai.py` — 5 error-return sites

**Files:**
- Modify: `markbot/providers/azure_openai.py` (5 sites)

- [ ] **Step 2.1: Read `azure_openai.py` to confirm 5 error-return sites**

Read the file and find each site:
- Non-stream status check (around line 151-155)
- Non-stream except (around line 161-164)
- Stream status check (around line 213-214)
- Stream status retry (around line 244-246)
- Stream except (around line 249)

- [ ] **Step 2.2: Add the import**

At the top of `markbot/providers/azure_openai.py`, add (next to other markbot imports):

```python
from markbot.providers.errors import ErrorType, classify_error
```

- [ ] **Step 2.3: Update non-stream status check (line 151-155)**

Find the pattern that returns an error response for non-200 status. It looks like:

```python
            if response.status_code != 200:
                return LLMResponse(
                    content=f"Azure OpenAI API Error {response.status_code}: {response.text}",
                    finish_reason="error",
                )
```

Replace with:

```python
            if response.status_code != 200:
                return LLMResponse(
                    content=f"Azure OpenAI API Error {response.status_code}: {response.text}",
                    finish_reason="error",
                    error_type=classify_error(response.status_code, response.text),
                )
```

- [ ] **Step 2.4: Update non-stream except (line 161-164)**

Find the pattern:

```python
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Azure OpenAI: {e}",
                finish_reason="error",
            )
```

Replace with:

```python
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Azure OpenAI: {e}",
                finish_reason="error",
                error_type=classify_error(None, repr(e)),
            )
```

- [ ] **Step 2.5: Update stream variant status check (line 213-214)**

Find the stream path version of the status check. It will look identical to the non-stream version except possibly wrapped differently. Apply the same change: add `error_type=classify_error(response.status_code, response.text)` to the `LLMResponse` constructor.

- [ ] **Step 2.6: Update stream variant status check inside retry loop (line 244-246)**

Same pattern as Step 2.5, but located inside a `for` loop. Apply the same change.

- [ ] **Step 2.7: Update stream variant except (line 249)**

Find the `except Exception as e:` in the stream path. Apply the same change as Step 2.4.

- [ ] **Step 2.8: Run regression test**

Run: `cd D:\Source\markbot; python -m pytest tests/ -k "azure" -v 2>&1 | tail -10`
Expected: All azure-related tests pass (or no tests selected, in which case the file just imports correctly)

Also run: `cd D:\Source\markbot; python -c "from markbot.providers.azure_openai import AzureOpenAIProvider; print('OK')"`
Expected: `OK`

- [ ] **Step 2.9: Commit**

```bash
cd D:\Source\markbot; git add markbot/providers/azure_openai.py; git commit -m "fix(providers): classify azure_openai errors via ErrorType"
```

---

## Task 3: Migrate `openai_compat.py` — 4 error-return sites (3 errors + 1 content_filter)

**Files:**
- Modify: `markbot/providers/openai_compat.py` (4 sites)

- [ ] **Step 3.1: Read `openai_compat.py` to confirm 4 error-return sites**

Find:
- Line 401: empty choices (chat)
- Line 449: empty choices (stream)
- Line 562-578: `finish_reason == "content_filter"` mapping
- Line 591: catch-all in stream path

- [ ] **Step 3.2: Add the import**

At the top of `markbot/providers/openai_compat.py`, add:

```python
from markbot.providers.errors import ErrorType, classify_error
```

- [ ] **Step 3.3: Update empty choices (chat) site (line 401)**

Find the pattern (returns an `LLMResponse(..., finish_reason="error")` when `choices` is empty). Add `error_type=ErrorType.UNKNOWN` to the constructor:

```python
            return LLMResponse(
                content="<error: empty choices>",
                finish_reason="error",
                error_type=ErrorType.UNKNOWN,
            )
```

- [ ] **Step 3.4: Update empty choices (stream) site (line 449)**

Same as Step 3.3 for the stream path. Add `error_type=ErrorType.UNKNOWN`.

- [ ] **Step 3.5: Update `content_filter` mapping (line 562-578)**

Find the block that maps a `content_filter` finish reason from the API to a response. It should already set `finish_reason="content_filter"`. Add `error_type=ErrorType.CONTENT`:

```python
            return LLMResponse(
                content="<error: content_filter>",
                finish_reason="content_filter",
                error_type=ErrorType.CONTENT,
            )
```

(Adjust the `content` text to match the existing format if different — the important change is the new `error_type` field.)

- [ ] **Step 3.6: Update catch-all in stream path (line 591)**

Find the catch-all `except` or empty-choices block in the streaming function. Add `error_type=ErrorType.UNKNOWN`.

- [ ] **Step 3.7: Run regression test**

Run: `cd D:\Source\markbot; python -c "from markbot.providers.openai_compat import OpenAICompatProvider; print('OK')"`
Expected: `OK`

Run: `cd D:\Source\markbot; python -m pytest tests/ -k "openai_compat" -v 2>&1 | tail -5`
Expected: All openai_compat tests pass (or no tests selected)

- [ ] **Step 3.8: Commit**

```bash
cd D:\Source\markbot; git add markbot/providers/openai_compat.py; git commit -m "fix(providers): classify openai_compat errors via ErrorType"
```

---

## Task 4: Migrate `anthropic.py` — 2 error-return sites

**Files:**
- Modify: `markbot/providers/anthropic.py` (2 sites)

- [ ] **Step 4.1: Read `anthropic.py` to confirm 2 sites**

Find:
- Line 428: `except Exception as e` in chat
- Line 453: `except Exception as e` in stream

- [ ] **Step 4.2: Add the import**

```python
from markbot.providers.errors import ErrorType, classify_error
```

- [ ] **Step 4.3: Update chat except (line 428)**

Find:

```python
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Anthropic: {e}",
                finish_reason="error",
            )
```

Replace with:

```python
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Anthropic: {e}",
                finish_reason="error",
                error_type=classify_error(None, repr(e)),
            )
```

- [ ] **Step 4.4: Update stream except (line 453)**

Apply the same change as Step 4.3 in the streaming function.

- [ ] **Step 4.5: Run regression test**

Run: `cd D:\Source\markbot; python -c "from markbot.providers.anthropic import AnthropicProvider; print('OK')"`
Expected: `OK`

- [ ] **Step 4.6: Commit**

```bash
cd D:\Source\markbot; git add markbot/providers/anthropic.py; git commit -m "fix(providers): classify anthropic errors via ErrorType"
```

---

## Task 5: Migrate `openai_codex.py` — 1 error-return site

**Files:**
- Modify: `markbot/providers/openai_codex.py` (1 site)

- [ ] **Step 5.1: Read `openai_codex.py` to confirm 1 site (line 77)**

Find the `except Exception as e` that wraps `_request_codex`.

- [ ] **Step 5.2: Add the import**

```python
from markbot.providers.errors import ErrorType, classify_error
```

- [ ] **Step 5.3: Update the except block (line 77)**

Find:

```python
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Codex: {e}",
                finish_reason="error",
            )
```

Replace with:

```python
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Codex: {e}",
                finish_reason="error",
                error_type=classify_error(None, repr(e)),
            )
```

(Adjust content text to match the existing format — the important change is the new `error_type` field.)

- [ ] **Step 5.4: Run regression test**

Run: `cd D:\Source\markbot; python -c "from markbot.providers.openai_codex import OpenAICodexProvider; print('OK')"`
Expected: `OK`

- [ ] **Step 5.5: Commit**

```bash
cd D:\Source\markbot; git add markbot/providers/openai_codex.py; git commit -m "fix(providers): classify openai_codex errors via ErrorType"
```

---

## Task 6: Migrate `base.py` — 2 catch-all sites + delete `_is_transient_error` helper

**Files:**
- Modify: `markbot/providers/base.py` (3 changes: 2 catch-alls + 1 helper deletion)

- [ ] **Step 6.1: Update `_safe_chat` catch-all (line 229)**

Find:

```python
    async def _safe_chat(self, **kwargs: Any) -> LLMResponse:
        """Call chat() and convert unexpected exceptions to error responses."""
        try:
            return await self.chat(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")
```

Replace with:

```python
    async def _safe_chat(self, **kwargs: Any) -> LLMResponse:
        """Call chat() and convert unexpected exceptions to error responses."""
        try:
            return await self.chat(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(
                content=f"Error calling LLM: {exc}",
                finish_reason="error",
                error_type=classify_error(None, repr(exc)),
            )
```

- [ ] **Step 6.2: Update `_safe_chat_stream` catch-all (line 265)**

Find:

```python
    async def _safe_chat_stream(self, **kwargs: Any) -> LLMResponse:
        """Call chat_stream() and convert unexpected exceptions to error responses."""
        try:
            return await self.chat_stream(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")
```

Replace with:

```python
    async def _safe_chat_stream(self, **kwargs: Any) -> LLMResponse:
        """Call chat_stream() and convert unexpected exceptions to error responses."""
        try:
            return await self.chat_stream(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(
                content=f"Error calling LLM: {exc}",
                finish_reason="error",
                error_type=classify_error(None, repr(exc)),
            )
```

- [ ] **Step 6.3: Delete `_TRANSIENT_ERROR_MARKERS` and `_is_transient_error`**

In `markbot/providers/base.py`:

1. Delete the class constant `LLMProvider._TRANSIENT_ERROR_MARKERS` (the tuple at the class level, around lines 82-91).

2. Delete the `_is_transient_error` classmethod (around lines 195-198).

- [ ] **Step 6.4: Update both retry-loop call sites to use `error_type`**

There are two retry loops that currently call `self._is_transient_error(response.content)`:

**Site 1** (around line 299, in `chat_stream_with_retry`):

Find:
```python
            if not self._is_transient_error(response.content):
                stripped = self._strip_image_content(messages)
                if stripped is not None:
                    logger.warning("Non-transient LLM error with image content, retrying without images")
                    return await self._safe_chat_stream(**{**kw, "messages": stripped})
                return response
```

Replace with:
```python
            if response.error_type != ErrorType.TRANSIENT:
                stripped = self._strip_image_content(messages)
                if stripped is not None:
                    logger.warning("Non-transient LLM error with image content, retrying without images")
                    return await self._safe_chat_stream(**{**kw, "messages": stripped})
                return response
```

**Site 2** (around line 350, in `chat_with_retry`):

Apply the identical change. Find:
```python
            if not self._is_transient_error(response.content):
                stripped = self._strip_image_content(messages)
                if stripped is not None:
                    logger.warning("Non-transient LLM error with image content, retrying without images")
                    return await self._safe_chat(**{**kw, "messages": stripped})
                return response
```

Replace with:
```python
            if response.error_type != ErrorType.TRANSIENT:
                stripped = self._strip_image_content(messages)
                if stripped is not None:
                    logger.warning("Non-transient LLM error with image content, retrying without images")
                    return await self._safe_chat(**{**kw, "messages": stripped})
                return response
```

- [ ] **Step 6.5: Verify `ErrorType` is imported**

Confirm `from markbot.providers.errors import ErrorType` is at the top of `base.py` (added in Task 1.3). If `classify_error` is not imported yet, add it:

```python
from markbot.providers.errors import ErrorType, classify_error
```

- [ ] **Step 6.6: Run regression test**

Run: `cd D:\Source\markbot; python -c "from markbot.providers.base import LLMProvider, LLMResponse; print('OK')"`
Expected: `OK`

Run: `cd D:\Source\markbot; python -m pytest tests/test_provider_errors.py tests/test_providers.py -v 2>&1 | tail -10`
Expected: All PASS (LLMResponse field default test, plus the original 4 LLMResponse tests)

- [ ] **Step 6.7: Commit**

```bash
cd D:\Source\markbot; git add markbot/providers/base.py; git commit -m "refactor(providers): use response.error_type in base.py retry loops"
```

---

## Task 7: Migrate `fallback.py` — delete 2 helpers + use `error_type` in try-next chain

**Files:**
- Modify: `markbot/providers/fallback.py` (multiple changes: delete 3 helpers, update 5 call sites)

- [ ] **Step 7.1: Add the import**

At the top of `markbot/providers/fallback.py`, add:

```python
from markbot.providers.errors import ErrorType, classify_error
```

- [ ] **Step 7.2: Delete `MODEL_UNAVAILABLE_ERRORS` constant**

In `markbot/providers/fallback.py`, find and delete the class constant:

```python
    MODEL_UNAVAILABLE_ERRORS = (
        "402", "insufficient balance", "insufficient_quota", "quota exceeded",
        "401", "unauthorized", "invalid api key", "authentication",
        "403", "forbidden", "access denied",
        "model not found", "model_not_found",
        "invalid function arguments", "invalid params",
    )
```

- [ ] **Step 7.3: Delete `_TRANSIENT_ERROR_MARKERS` constant**

Find and delete:

```python
    _TRANSIENT_ERROR_MARKERS = (
        "timeout", "timed out", "connection", "rate limit", "rate_limit",
        "too many requests", "server error", "internal server error",
        "service unavailable", "overloaded", "capacity", "busy",
        "try again", "retry", "503", "502", "504", "529",
        "temporarily unavailable", "throttle",
    )
```

- [ ] **Step 7.4: Delete `_is_retryable_error` static method**

Find and delete:

```python
    @staticmethod
    def _is_retryable_error(error: Exception | str) -> bool:
        err_str = str(error).lower()
        if not err_str or err_str.isspace():
            return True
        return any(marker in err_str for marker in FallbackManager._TRANSIENT_ERROR_MARKERS)
```

- [ ] **Step 7.5: Delete `_is_model_unavailable_error` static method**

Find and delete:

```python
    @staticmethod
    def _is_model_unavailable_error(error: Exception | str) -> bool:
        err_str = str(error).lower()
        return any(marker in err_str for marker in FallbackManager.MODEL_UNAVAILABLE_ERRORS)
```

- [ ] **Step 7.6: Update try-next chain when response.finish_reason == "error" (lines 222-236)**

Find:

```python
                    if self._is_retryable_error(error_msg):
                        logger.warning(
                            "Model {} returned error (retryable): {}. Trying next...",
                            model_ref, error_msg,
                        )
                    elif self._is_model_unavailable_error(error_msg):
                        logger.warning(
                            "Model {} unavailable: {}. Trying next...",
                            model_ref, error_msg,
                        )
                    else:
                        logger.error(
                            "Model {} returned error (non-retryable): {}. Trying next...",
                            model_ref, error_msg,
                        )
```

Replace with:

```python
                    if response.error_type == ErrorType.TRANSIENT:
                        logger.warning(
                            "Model {} returned error (retryable): {}. Trying next...",
                            model_ref, error_msg,
                        )
                    elif response.error_type == ErrorType.UNAVAILABLE:
                        logger.warning(
                            "Model {} unavailable: {}. Trying next...",
                            model_ref, error_msg,
                        )
                    else:
                        logger.error(
                            "Model {} returned error (non-retryable): {}. Trying next...",
                            model_ref, error_msg,
                        )
```

- [ ] **Step 7.7: Update `except Exception` branch (lines 280-292)**

Find:

```python
                if self._is_retryable_error(e):
                    logger.warning(
                        "Model {} failed (retryable): {}. Trying next...",
                        model_ref, e,
                    )
                elif self._is_model_unavailable_error(e):
                    logger.warning(
                        "Model {} unavailable: {}. Trying next...", model_ref, e,
                    )
                else:
                    logger.error(
                        "Model {} failed (non-retryable): {}. Trying next...",
                        model_ref, e,
                    )
```

Replace with:

```python
                err_type = classify_error(None, str(e))
                if err_type == ErrorType.TRANSIENT:
                    logger.warning(
                        "Model {} failed (retryable): {}. Trying next...",
                        model_ref, e,
                    )
                elif err_type == ErrorType.UNAVAILABLE:
                    logger.warning(
                        "Model {} unavailable: {}. Trying next...", model_ref, e,
                    )
                else:
                    logger.error(
                        "Model {} failed (non-retryable): {}. Trying next...",
                        model_ref, e,
                    )
```

- [ ] **Step 7.8: Verify no remaining references to the deleted helpers**

Run: `cd D:\Source\markbot; python -c "from markbot.providers.fallback import FallbackManager; assert not hasattr(FallbackManager, '_is_retryable_error'); assert not hasattr(FallbackManager, '_is_model_unavailable_error'); assert not hasattr(FallbackManager, 'MODEL_UNAVAILABLE_ERRORS'); assert not hasattr(FallbackManager, '_TRANSIENT_ERROR_MARKERS'); print('OK')"`
Expected: `OK`

- [ ] **Step 7.9: Run regression test**

Run: `cd D:\Source\markbot; python -m pytest tests/test_fallback.py -v --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default 2>&1 | tail -10`
Expected: 5 of 6 tests pass (test_circuit_threshold_default is the pre-existing failure)

- [ ] **Step 7.10: Commit**

```bash
cd D:\Source\markbot; git add markbot/providers/fallback.py; git commit -m "refactor(providers): use response.error_type in fallback.py try-next chain"
```

---

## Task 8: Update `tests/test_providers.py` — rewrite 5 tests to use `classify_error`

**Files:**
- Modify: `tests/test_providers.py` (replace `TestFallbackManager` substring tests)

- [ ] **Step 8.1: Update test imports**

At the top of `tests/test_providers.py`, add:

```python
from markbot.providers.errors import ErrorType, classify_error
```

- [ ] **Step 8.2: Rewrite the 5 substring-based tests in `TestFallbackManager`**

Find the `TestFallbackManager` class (around lines 88-122) and replace the 5 `_is_retryable_error` / `_is_model_unavailable_error` tests (lines 89-106) with:

```python
class TestFallbackManager:
    def test_classify_timeout_is_transient(self):
        assert classify_error(None, "connection timeout") == ErrorType.TRANSIENT

    def test_classify_rate_limit_is_transient(self):
        assert classify_error(None, "rate limit exceeded") == ErrorType.TRANSIENT

    def test_classify_503_is_transient(self):
        assert classify_error(503, "service unavailable") == ErrorType.TRANSIENT

    def test_classify_invalid_api_key_is_unavailable(self):
        assert classify_error(None, "invalid api key") == ErrorType.UNAVAILABLE

    def test_classify_402_is_unavailable(self):
        assert classify_error(402, "insufficient balance") == ErrorType.UNAVAILABLE

    def test_classify_401_is_unavailable(self):
        assert classify_error(401, "unauthorized") == ErrorType.UNAVAILABLE

    def test_classify_model_not_found_is_unavailable(self):
        assert classify_error(None, "model not found") == ErrorType.UNAVAILABLE

    def test_classify_unknown_message_is_unknown(self):
        assert classify_error(None, "totally unrecognised error") == ErrorType.UNKNOWN
```

Keep the `test_circuit_breaker_flow` test that follows (unchanged).

- [ ] **Step 8.3: Run updated tests**

Run: `cd D:\Source\markbot; python -m pytest tests/test_providers.py -v 2>&1 | tail -15`
Expected: All 8 new `classify_*` tests PASS, plus all other existing tests in the file still pass (LLMResponse, CircuitState, FallbackAttempt, ProviderSpec, DeepSeekSpec, etc.)

- [ ] **Step 8.4: Commit**

```bash
cd D:\Source\markbot; git add tests/test_providers.py; git commit -m "test(providers): rewrite substring tests to assert on ErrorType enum"
```

---

## Task 9: Final verification

**Files:** (no new files; verification only)

- [ ] **Step 9.1: Run full test suite**

Run: `cd D:\Source\markbot; python -m pytest --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default -q 2>&1 | tail -3`
Expected: All tests pass (885 + 8 from Task 8, plus ~30 from Task 1 = ~920 tests; one deselected)

- [ ] **Step 9.2: Run ruff on changed files**

Run: `cd D:\Source\markbot; python -m ruff check markbot/providers/ tests/test_provider_errors.py tests/test_providers.py 2>&1 | tail -5`
Expected: All checks passed

- [ ] **Step 9.3: Verify no remaining references to deleted helpers**

Run: `cd D:\Source\markbot; grep -rn "_TRANSIENT_ERROR_MARKERS\|_is_transient_error\|_is_retryable_error\|_is_model_unavailable_error\|MODEL_UNAVAILABLE_ERRORS" markbot/ tests/ 2>&1 | grep -v ".pyc" | head -10`
Expected: No output (all references removed)

- [ ] **Step 9.4: Verify `git log` shows 8 new commits on top of P1-3**

Run: `cd D:\Source\markbot; git log --oneline -12`
Expected: 8 P1-4 commits visible, each with a `feat(...)` / `refactor(...)` / `fix(...)` / `test(...)` message

- [ ] **Step 9.5: Commit any pending cleanup (if needed)**

If any uncommitted changes remain:
```bash
cd D:\Source\markbot; git status; git add -A; git commit -m "chore: p1-4 final cleanup"
```

---

## Self-Review

**Spec coverage check:**
- New `markbot/providers/errors.py` with `ErrorType` + `classify_error` → Task 1 ✓
- `LLMResponse.error_type` field added → Task 1.3 ✓
- 5 azure_openai sites → Task 2 ✓
- 4 openai_compat sites → Task 3 ✓
- 2 anthropic sites → Task 4 ✓
- 1 openai_codex site → Task 5 ✓
- 2 base.py catch-alls + 1 retry-loop helper deletion + 2 retry-loop call-site updates → Task 6 ✓
- Delete `MODEL_UNAVAILABLE_ERRORS`, `_TRANSIENT_ERROR_MARKERS`, `_is_retryable_error`, `_is_model_unavailable_error` in fallback.py + 2 call-site updates → Task 7 ✓
- Rewrite 5 substring tests in `tests/test_providers.py` → Task 8 ✓
- New `tests/test_provider_errors.py` with 25+ unit tests → Task 1 ✓
- Final verification → Task 9 ✓

**Placeholder scan:** No TBD/TODO. All test code is shown. All imports are spelled out. All file paths are exact.

**Type consistency:**
- `ErrorType.TRANSIENT | UNAVAILABLE | CONTENT | UNKNOWN` used consistently in Tasks 1, 6, 7 ✓
- `classify_error(status_code, message)` signature used consistently in Tasks 1-7 ✓
- `response.error_type` access pattern used consistently in Tasks 6, 7 ✓
- `LLMResponse(..., error_type=...)` constructor pattern used in all 13 provider sites ✓
