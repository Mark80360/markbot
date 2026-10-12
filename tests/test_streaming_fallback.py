"""Tests for streaming fallback.

Deltas already pushed to the user cannot be retracted. If an attempt failed
part-way through a stream and the fallback chain rotated to another model,
that model streamed into the *same* sink — so the user received the failed
attempt's partial text followed by the replacement's full output, with no
way to tell where one answer ended and the next began.

The loop already discarded the buffer on a ``content_filter`` hit; a
mid-stream transport failure needed the same treatment.
"""

from __future__ import annotations

import pytest

from markbot.agent.iteration import IterationRunner
from markbot.providers.base import LLMResponse, ToolCallRequest
from markbot.providers.fallback import FallbackAttempt


def _attempt(**kw) -> FallbackAttempt:
    base = {"model_ref": "a/model", "success": False, "error": "connection reset"}
    base.update(kw)
    return FallbackAttempt(**base)


class TestPollutedStreamDetection:
    def _runner(self):
        r = object.__new__(IterationRunner)
        return r

    def test_content_filter_pollutes(self):
        assert "content_filter" in self._runner()._polluted_stream(
            [_attempt(error="content_filter")]
        )

    def test_streamed_failure_pollutes(self):
        """The case this fix adds: no content filter, but bytes went out."""
        reason = self._runner()._polluted_stream(
            [_attempt(error="connection reset", streamed=True)]
        )
        assert reason
        assert "mid-flight" in reason

    def test_clean_failure_does_not_pollute(self):
        """A failure before any byte was emitted leaves nothing to discard."""
        assert self._runner()._polluted_stream(
            [_attempt(error="connection reset", streamed=False)]
        ) == ""

    def test_success_does_not_pollute(self):
        assert self._runner()._polluted_stream(
            [FallbackAttempt(model_ref="a/m", success=True)]
        ) == ""

    def test_no_attempts(self):
        assert self._runner()._polluted_stream([]) == ""
        assert self._runner()._polluted_stream(None) == ""

    def test_first_polluting_attempt_wins(self):
        reason = self._runner()._polluted_stream([
            _attempt(error="content_filter", streamed=False),
            _attempt(error="timeout", streamed=True),
        ])
        assert "content_filter" in reason

    def test_handles_attempt_without_streamed_attr(self):
        """Older attempt objects must not raise."""

        class Legacy:
            error = "boom"
            success = False

        assert self._runner()._polluted_stream([Legacy()]) == ""


class TestAttemptFlag:
    def test_streamed_defaults_false(self):
        assert FallbackAttempt(model_ref="a/m").streamed is False

    def test_streamed_is_settable(self):
        a = FallbackAttempt(model_ref="a/m", success=False, error="x")
        a.streamed = True
        assert a.streamed is True


class TestNoDuplicateContent:
    async def test_failed_stream_does_not_emit_after_failure(self, monkeypatch):
        """The provider must stop pushing deltas once the attempt fails."""
        import markbot.providers.openai_compat as mod

        pushed: list[str] = []

        class _Chunk:
            def __init__(self, text):
                self.choices = [type(
                    "C", (), {"delta": type("D", (), {"content": text})}
                )()]

        class _Stream:
            def __init__(self):
                self._n = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                self._n += 1
                if self._n == 1:
                    return _Chunk("partial ")
                raise httpx_read_error()

        def httpx_read_error():
            import httpx

            return httpx.ReadTimeout("stream died")

        p = mod.OpenAICompatProvider(api_key="k", spec=None)
        p._build_kwargs = lambda *a, **k: {"model": "m", "messages": []}

        async def _create(**kw):
            return _Stream()

        p._client = type("C", (), {
            "chat": type("Ch", (), {
                "completions": type("Co", (), {"create": staticmethod(_create)})
            })()
        })()

        async def _on_delta(text):
            pushed.append(text)

        out = await p.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            on_content_delta=_on_delta,
        )
        # One delta went out before the failure; nothing after.
        assert pushed == ["partial "]
        # And the response is flagged as an error so the loop rotates /
        # discards rather than treating the partial text as the answer.
        assert out.finish_reason == "error"


class TestProviderStopsDeltaOnError:
    async def test_no_deltas_after_exception(self, monkeypatch):
        import httpx

        import markbot.providers.openai_compat as mod

        pushed: list[str] = []

        class _Chunk:
            def __init__(self, text):
                self.choices = [type(
                    "C", (), {"delta": type("D", (), {"content": text})}
                )()]

        class _Stream:
            def __init__(self):
                self._n = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                self._n += 1
                if self._n <= 2:
                    return _Chunk(f"t{self._n}")
                raise httpx.ReadError("gone")

        p = mod.OpenAICompatProvider(api_key="k", spec=None)
        p._build_kwargs = lambda *a, **k: {"model": "m", "messages": []}
        p._client = type("C", (), {
            "chat": type("Ch", (), {
                "completions": type("Co", (), {
                    "create": staticmethod(lambda **kw: _async(_Stream()))
                })()
            })()
        })()

        async def _on_delta(text):
            pushed.append(text)

        out = await p.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            on_content_delta=_on_delta,
        )
        assert pushed == ["t1", "t2"]
        assert out.finish_reason == "error"


def _async(value):
    async def _inner():
        return value

    return _inner()
