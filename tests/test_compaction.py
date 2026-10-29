"""Tests for compaction service."""

import pytest

from markbot.agent.compact import (
    CompactAction,
    CompactResult,
    CompactionConfig,
    MultiLevelCompactor,
    is_prompt_too_long_error,
)
from markbot.errors import ContextWindowExceededError
from markbot.providers.fallback import AllModelsFailedError

SYSTEM = "SYSTEM PROMPT " + "x" * 5000


class TestCompactAction:
    def test_all_actions_exist(self):
        assert CompactAction.COLLAPSE
        assert CompactAction.MICRO_COMPACT
        assert CompactAction.AUTO_COMPACT
        assert CompactAction.HISTORY_SNIP
        assert CompactAction.NONE

    def test_action_ordering(self):
        assert CompactAction.NONE != CompactAction.COLLAPSE


class TestCompactResult:
    def test_default_values(self):
        result = CompactResult(
            action=CompactAction.NONE,
            messages_before=10,
            messages_after=10,
            tokens_before=1000,
            tokens_after=1000,
        )
        assert result.action == CompactAction.NONE
        assert result.messages_before == result.messages_after


class TestIsPromptTooLongError:
    def test_detects_context_length_error(self):
        assert is_prompt_too_long_error("context length exceeded") is True

    def test_detects_prompt_too_long(self):
        assert is_prompt_too_long_error("prompt too long for model") is True

    def test_detects_token_limit(self):
        assert is_prompt_too_long_error("too many tokens in request") is True

    def test_ignores_unrelated_error(self):
        assert is_prompt_too_long_error("connection timeout") is False

    def test_detects_exceed_context(self):
        assert is_prompt_too_long_error("exceed_context limit") is True

    def test_case_insensitive(self):
        assert is_prompt_too_long_error("Prompt Too Long") is True

    def test_none_returns_false(self):
        assert is_prompt_too_long_error(None) is False

    def test_empty_string_returns_false(self):
        assert is_prompt_too_long_error("") is False

    def test_unwraps_fallback_wrapper(self):
        """A context overflow must survive ``AllModelsFailedError``.

        The wrapper only renders "All N models in chain failed"; without
        walking the cause chain reactive compaction never fires.
        """
        wrapped = AllModelsFailedError(
            "All 1 models in chain failed",
            last_error=ContextWindowExceededError("prompt is too long"),
        )
        assert is_prompt_too_long_error(wrapped) is True

    def test_does_not_misclassify_other_wrapped_errors(self):
        wrapped = AllModelsFailedError(
            "All 1 models in chain failed",
            last_error=RuntimeError("connection timeout"),
        )
        assert is_prompt_too_long_error(wrapped) is False


class TestSystemPromptPreserved:
    """Compaction must never rewrite or drop the system prompt."""

    def _compactor(self) -> MultiLevelCompactor:
        return MultiLevelCompactor(
            config=CompactionConfig(
                collapse_tool_result_chars=200,
                collapse_head_chars=80,
                collapse_tail_chars=40,
                auto_compact_keep_recent=3,
                snip_keep_messages=4,
            )
        )

    def test_collapse_skips_system(self):
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "tool", "content": "t" * 3000},
        ]
        out = self._compactor()._apply_collapse([dict(m) for m in messages])

        assert out[0]["role"] == "system"
        assert out[0]["content"] == SYSTEM
        assert len(out[1]["content"]) < 3000

    def test_history_snip_keeps_system(self):
        messages = [{"role": "system", "content": SYSTEM}] + [
            {"role": "user", "content": f"m{i}"} for i in range(30)
        ]
        out = self._compactor()._apply_history_snip([dict(m) for m in messages])

        assert out[0]["role"] == "system"
        assert out[0]["content"] == SYSTEM
        assert len(out) == 6  # system + notice + 4 kept

    @pytest.mark.asyncio
    async def test_auto_compaction_keeps_system(self):
        messages = [{"role": "system", "content": SYSTEM}] + [
            {"role": "user", "content": f"m{i} " + "z" * 200} for i in range(20)
        ]
        out, _summary = await self._compactor()._apply_auto_compaction(
            [dict(m) for m in messages]
        )

        assert out[0]["role"] == "system"
        assert out[0]["content"] == SYSTEM
