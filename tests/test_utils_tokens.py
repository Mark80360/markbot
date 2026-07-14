"""Tests for markbot.utils.tokens — token estimation utilities."""

from __future__ import annotations

import pytest

from markbot.utils.tokens import estimate_messages_tokens, estimate_tokens


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_none_returns_zero(self):
        assert estimate_tokens(None) == 0

    def test_non_empty_returns_positive(self):
        assert estimate_tokens("Hello, world!") > 0

    def test_longer_text_more_tokens(self):
        short = estimate_tokens("Hi")
        long = estimate_tokens("This is a much longer piece of text with many more words.")
        assert long > short

    def test_unicode_text(self):
        result = estimate_tokens("你好世界")
        assert result > 0

    def test_whitespace_only(self):
        result = estimate_tokens("    ")
        assert result >= 0

    def test_code_snippet(self):
        code = "def hello():\n    print('world')\n"
        result = estimate_tokens(code)
        assert result > 0


# ---------------------------------------------------------------------------
# estimate_messages_tokens
# ---------------------------------------------------------------------------


class TestEstimateMessagesTokens:
    def test_empty_list(self):
        assert estimate_messages_tokens([]) == 0

    def test_single_message(self):
        msgs = [{"role": "user", "content": "Hello!"}]
        result = estimate_messages_tokens(msgs)
        assert result > 0
        # Should include per-message overhead (4 tokens)
        assert result >= 4

    def test_multiple_messages(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        result = estimate_messages_tokens(msgs)
        assert result >= 12  # 3 messages × 4 overhead

    def test_non_dict_entry_skipped(self):
        msgs = ["not a dict", {"role": "user", "content": "Hi"}]
        result = estimate_messages_tokens(msgs)
        assert result >= 4

    def test_content_as_list(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello world"},
                ],
            }
        ]
        result = estimate_messages_tokens(msgs)
        assert result > 4  # overhead + text tokens

    def test_tool_use_block(self):
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "input": "ls -la"},
                ],
            }
        ]
        result = estimate_messages_tokens(msgs)
        assert result > 4

    def test_tool_result_block(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "file1.txt\nfile2.txt"},
                ],
            }
        ]
        result = estimate_messages_tokens(msgs)
        assert result > 4

    def test_tool_calls_openai_style(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "function": {"name": "read", "arguments": '{"path": "/tmp/file.txt"}'}}],
            }
        ]
        result = estimate_messages_tokens(msgs)
        assert result > 4

    def test_missing_content_key(self):
        msgs = [{"role": "user"}]
        result = estimate_messages_tokens(msgs)
        assert result == 4  # just overhead

    def test_none_content(self):
        msgs = [{"role": "user", "content": None}]
        result = estimate_messages_tokens(msgs)
        assert result == 4  # just overhead

    def test_empty_content_string(self):
        msgs = [{"role": "user", "content": ""}]
        result = estimate_messages_tokens(msgs)
        assert result == 4  # just overhead

    def test_more_messages_more_tokens(self):
        single = estimate_messages_tokens([{"role": "user", "content": "test"}])
        triple = estimate_messages_tokens(
            [{"role": "user", "content": "test"}] * 3
        )
        assert triple > single
