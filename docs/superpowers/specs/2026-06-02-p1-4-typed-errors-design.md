# P1-4: Typed Error Classification for LLM Provider Responses — Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:writing-plans to create the implementation plan after this spec is approved.

**Goal:** Replace ad-hoc substring matching against `_TRANSIENT_ERROR_MARKERS` and `MODEL_UNAVAILABLE_ERRORS` with a structured `LLMResponse.error_type` field that providers fill in at the point of error construction, and callers consume via simple `is`-style checks.

**Architecture:** New module `markbot/providers/errors.py` defines a 4-value `ErrorType` enum (TRANSIENT / UNAVAILABLE / CONTENT / UNKNOWN) and a `classify_error(status_code, message)` function that prefers HTTP status codes (precise) and falls back to message-substring hints (legacy support). Each of the 13 error-return sites in 5 provider files is updated to call `classify_error` and store the result on the response. The 3 call sites in `base.py` and `fallback.py` then drop their `_is_transient_error` / `_is_retryable_error` / `_is_model_unavailable_error` helpers and use the field directly.

**Tech Stack:** Python 3.13 stdlib only (`enum.Enum`), `httpx` (already used), pytest (existing).

---

## 1. Problem

The current retry / fallback logic in `markbot/providers/base.py` and `markbot/providers/fallback.py` classifies LLM errors by substring-matching the error message text:

```python
# base.py:82
_TRANSIENT_ERROR_MARKERS = (
    "429", "529", "rate limit", "500", "502", "503", "504",
    "overloaded", "timeout", "timed out", "connection",
    "server error", "temporarily unavailable",
)
```

```python
# fallback.py:64
_TRANSIENT_ERROR_MARKERS = (
    "timeout", "timed out", "connection", "rate limit", "rate_limit",
    "too many requests", "server error", "internal server error",
    "service unavailable", "overloaded", "capacity", "busy",
    "try again", "retry", "503", "502", "504", "529",
    "temporarily unavailable", "throttle",
)
```

```python
# fallback.py:56
MODEL_UNAVAILABLE_ERRORS = (
    "402", "insufficient balance", "insufficient_quota", "quota exceeded",
    "401", "unauthorized", "invalid api key", "authentication",
    "403", "forbidden", "access denied",
    "model not found", "model_not_found",
    "invalid function arguments", "invalid params",
)
```

The two `_TRANSIENT_ERROR_MARKERS` tuples are **diverged copies** with different contents (13 vs 19 items, only partial overlap), so the same error can be classified differently by `LLMProvider` vs `FallbackManager`. The substring matcher is also fragile against providers that wrap errors in different phrasings, locale translations, or wrapper-class names.

The 13 error-return sites across 5 provider files currently embed the HTTP status code in the error message text (e.g. `f"Azure OpenAI API Error {response.status_code}: {response.text}"`), so the substring matcher can recover it. But the structure is lost, and any change to message format breaks the classifier silently.

## 2. Goals & Non-Goals

**Goals**
- One source of truth for error classification (`ErrorType` enum + `classify_error`)
- HTTP status code is the primary signal (precise, language-independent)
- Substring matching remains as a fallback for paths where status is unavailable (connection errors, timeouts, Python exceptions raised before the HTTP response)
- Public API of `LLMProvider` and `FallbackManager` is unchanged — same method signatures, same return types, same retry/fallback behavior
- All 5 pre-existing substring-based tests in `tests/test_providers.py` are updated to assert on `ErrorType` enum values rather than string content

**Non-Goals**
- Provider exceptions (raising instead of returning a response) — too invasive
- Configurable error markers — operators do not need to tune these
- Per-provider subclassing of `ErrorType` (e.g. `AnthropicError` vs `OpenAIError`) — the 4-value classification is sufficient
- Distinguishing sub-categories of TRANSIENT (rate-limit vs server-overload vs timeout) — collapsed into one bucket per user decision
- Changing `finish_reason` semantics — it stays `"error"` / `"content_filter"`; `error_type` is a parallel signal
- Removing the error message text from `LLMResponse.content` — the message is still useful for logs

## 3. Design

### 3.1 New module: `markbot/providers/errors.py`

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

### 3.2 `LLMResponse.error_type` field

```python
# markbot/providers/base.py
@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None
    thinking_blocks: list[dict] | None = None
    error_type: ErrorType | None = None  # NEW: set when finish_reason in {"error", "content_filter"}
```

The field is `None` on success. On error it is set by the provider at the moment the response is constructed. Backward compatible — existing code that reads `finish_reason` and `content` continues to work.

### 3.3 Provider error-return sites (13 total)

Each of the 13 sites that currently returns `LLMResponse(..., finish_reason="error")` (or `"content_filter"`) is updated to fill in `error_type` via `classify_error`:

**`markbot/providers/azure_openai.py`** — 5 sites, all have access to `response.status_code`:
- Line 151-155: `response.status_code != 200` → `error_type=classify_error(response.status_code, response.text)`
- Line 161-164: `except Exception as e` → `error_type=classify_error(None, repr(e))`
- Line 213-214: stream variant of status check
- Line 244-246: stream variant of `response.status_code` check
- Line 249: stream variant of `except` block

**`markbot/providers/openai_compat.py`** — 3 sites, plus the `content_filter` mapping:
- Line 401: empty choices (chat) → `error_type=ErrorType.UNKNOWN`
- Line 449: empty choices (stream) → `error_type=ErrorType.UNKNOWN`
- Line 562-578: `finish_reason == "content_filter"` mapping → set `finish_reason="content_filter"` **and** `error_type=ErrorType.CONTENT`
- Line 591: catch-all in stream path → `error_type=ErrorType.UNKNOWN`

**`markbot/providers/anthropic.py`** — 2 sites (no status code available at the catch point, message only):
- Line 428: `except Exception as e` in chat
- Line 453: `except Exception as e` in stream

**`markbot/providers/openai_codex.py`** — 1 site:
- Line 77: `except Exception as e` wrapping `_request_codex` call

**`markbot/providers/base.py`** — 2 sites in `_safe_chat` / `_safe_chat_stream`:
- Line 229, 265: `except Exception as exc` → `error_type=classify_error(None, repr(exc))`

### 3.4 Caller updates (5 sites in 2 files)

The three existing classification helpers and their constants are deleted:

```
LLMProvider._TRANSIENT_ERROR_MARKERS        (base.py:82)
LLMProvider._is_transient_error()           (base.py:195-198)
FallbackManager._TRANSIENT_ERROR_MARKERS    (fallback.py:64-70)
FallbackManager._is_retryable_error()       (fallback.py:82-87)
FallbackManager.MODEL_UNAVAILABLE_ERRORS    (fallback.py:56-62)
FallbackManager._is_model_unavailable_error()(fallback.py:130-133)
```

The 5 call sites that used them are updated to use `response.error_type` directly:

**`markbot/providers/base.py` (chat_with_retry / _stream retry loops, lines 299 & 350):**
```python
# before
if not self._is_transient_error(response.content):
    stripped = self._strip_image_content(messages)
    if stripped is not None:
        ...
    return response

# after
if response.error_type != ErrorType.TRANSIENT:
    stripped = self._strip_image_content(messages)
    if stripped is not None:
        ...
    return response
```

**`markbot/providers/fallback.py` (chat_with_fallback, lines 222-236 & 280-290):**
```python
# before
if self._is_retryable_error(error_msg):
    logger.warning("Model {} returned error (retryable): {}. ...", ...)
elif self._is_model_unavailable_error(error_msg):
    logger.warning("Model {} unavailable: {}. ...", ...)
else:
    logger.error("Model {} returned error (non-retryable): {}. ...", ...)

# after
if response.error_type == ErrorType.TRANSIENT:
    logger.warning("Model {} returned error (retryable): {}. ...", ...)
elif response.error_type == ErrorType.UNAVAILABLE:
    logger.warning("Model {} unavailable: {}. ...", ...)
else:
    logger.error("Model {} returned error (non-retryable): {}. ...", ...)
```

The same pattern applies to the `except Exception` branch on lines 280-290 — use the exception's `str(e)` to call `classify_error(None, str(e))` at the point of constructing the synthetic error response, or simply check `e` against `ErrorType.TRANSIENT` by classifying the message inline.

The cleanest approach: at the `except` site, classify the message and use the resulting `ErrorType` in the same `if/elif/else` chain. The error is **not** constructed as an `LLMResponse` in this branch (only logged), so the caller just needs the `ErrorType` value:

```python
except Exception as e:
    err_type = classify_error(None, str(e))
    if err_type == ErrorType.TRANSIENT:
        logger.warning("Model {} failed (retryable): {}. ...", ...)
    elif err_type == ErrorType.UNAVAILABLE:
        logger.warning("Model {} unavailable: {}. ...", ...)
    else:
        logger.error("Model {} failed (non-retryable): {}. ...", ...)
```

### 3.5 Test updates

`tests/test_providers.py` (5 tests) are updated to assert on the structured classifier output rather than the substring matcher:

| Old test | New assertion |
|---|---|
| `test_is_retryable_error_timeout` | `classify_error(None, "connection timeout") == ErrorType.TRANSIENT` |
| `test_is_retryable_error_rate_limit` | `classify_error(None, "rate limit exceeded") == ErrorType.TRANSIENT` |
| `test_is_retryable_error_503` | `classify_error(503, "service unavailable") == ErrorType.TRANSIENT` |
| `test_is_retryable_error_invalid_api_key` | `classify_error(None, "invalid api key") == ErrorType.UNAVAILABLE` |
| `test_is_model_unavailable_error` | Split into per-classifier assertions: `classify_error(402, "...") == ErrorType.UNAVAILABLE`, `classify_error(401, "...") == ErrorType.UNAVAILABLE`, `classify_error(None, "model not found") == ErrorType.UNAVAILABLE`, `classify_error(None, "timeout") == ErrorType.TRANSIENT` |

New tests added (in `tests/test_provider_errors.py`):
- `classify_error(429, ...) == TRANSIENT`
- `classify_error(500, ...) == TRANSIENT`
- `classify_error(529, ...) == TRANSIENT`
- `classify_error(401, ...) == UNAVAILABLE`
- `classify_error(402, ...) == UNAVAILABLE`
- `classify_error(403, ...) == UNAVAILABLE`
- `classify_error(404, ...) == UNAVAILABLE`
- `classify_error(None, "rate limit") == TRANSIENT`
- `classify_error(None, "model not found") == UNAVAILABLE`
- `classify_error(None, "content_filter") == CONTENT`
- `classify_error(None, "weird unrecognised text") == UNKNOWN`
- `classify_error(200, "ok") == UNKNOWN` (200 is not in the map, falls through to message match)
- `LLMResponse` field default is `None`
- `LLMResponse.error_type` round-trip via `__init__`

## 4. Backward Compatibility

- `LLMResponse` constructor with no `error_type` argument produces a response with `error_type=None` — identical to today's behavior
- `finish_reason` semantics unchanged: `"error"` and `"content_filter"` still drive the same control flow
- Provider call sites still return `LLMResponse` objects, never raise (caller contract preserved)
- `FallbackManager.chat_with_fallback` and `LLMProvider.chat_with_retry` signatures unchanged
- The substring-marker constants are removed — any external code that imported them would break, but a grep across the repo shows no such imports (verified)
- `tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default` — pre-existing failure unrelated to P1-4, requires `--deselect` as before

## 5. File Inventory

| File | Action | Lines (approx) |
|---|---|---|
| `markbot/providers/errors.py` | create | +80 |
| `markbot/providers/base.py` | modify | +5 / -15 |
| `markbot/providers/fallback.py` | modify | +10 / -25 |
| `markbot/providers/azure_openai.py` | modify | +10 / -5 |
| `markbot/providers/openai_compat.py` | modify | +8 / -3 |
| `markbot/providers/anthropic.py` | modify | +4 / -2 |
| `markbot/providers/openai_codex.py` | modify | +2 / -1 |
| `tests/test_providers.py` | modify | +5 / -10 |
| `tests/test_provider_errors.py` | create | +90 |

Net: ~+130 / -60 = +70 lines. All new behavior is in one new module + one new test file; provider call sites get a small one-line change at each error return.

## 6. Testing Strategy

1. **Unit tests** for `classify_error` (new `tests/test_provider_errors.py`): every status code in `_STATUS_TO_ERROR_TYPE`, every marker in `_ERROR_MESSAGE_HINTS`, the `UNKNOWN` fallthrough, and the `LLMResponse` field default
2. **Updated tests** in `tests/test_providers.py`: 5 existing tests rewritten to assert on `ErrorType` rather than substring
3. **Regression tests** for `FallbackManager`: 6 existing tests in `tests/test_fallback.py` continue to pass (excluding the pre-existing `test_circuit_threshold_default` failure which is unrelated to error classification)
4. **Full project test suite**: `python -m pytest --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default` must pass with 885+ tests (886 after P1-3, 895+ after P1-4)
5. **Lint**: `ruff check markbot/providers/ tests/test_provider_errors.py tests/test_providers.py` must pass

## 7. Out of Scope (Future)

- Per-provider `ErrorType` subclasses (e.g. `AnthropicError` vs `OpenAIError`) — not needed today
- Configurable error markers — operators have not asked for this and the static mapping covers all observed cases
- Metrics / observability for error type distribution — can be added later by reading `response.error_type` in fallback log handlers
- Provider-raised exceptions instead of returned responses — would be a much larger refactor; defer

## 8. Decisions Locked

| Decision | Choice |
|---|---|
| Where typed exception lands | `LLMResponse.error_type` field (not raising) |
| Granularity | 4 values: TRANSIENT / UNAVAILABLE / CONTENT / UNKNOWN |
| Classifier implementation | HTTP status_code primary + message fallback |
| Config override | Code constants only, no config exposure |
| `finish_reason` semantics | Unchanged |
| Test updates | Rewrite 5 existing tests + new `test_provider_errors.py` |
