"""Tests for compaction service."""

from markbot.agent.compact import (
    CompactAction,
    CompactResult,
    is_prompt_too_long_error,
)


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
