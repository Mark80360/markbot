"""Tests for markbot.agent.context — PromptSection and multimodal helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from markbot.agent.context import (
    ContextBuilder,
    DEFAULT_SYSTEM_PROMPT_TOKEN_BUDGET,
    PromptSection,
    unwrap_multimodal_result,
)


# ---------------------------------------------------------------------------
# PromptSection
# ---------------------------------------------------------------------------


class TestPromptSection:
    def test_defaults(self):
        s = PromptSection(content="hello", name="test")
        assert s.content == "hello"
        assert s.name == "test"
        assert s.priority == 3

    def test_custom_priority(self):
        s = PromptSection(content="hello", name="test", priority=1)
        assert s.priority == 1

    def test_tokens_positive(self):
        s = PromptSection(content="Hello, world!", name="test")
        assert s.tokens > 0

    def test_tokens_zero_for_empty(self):
        s = PromptSection(content="", name="test")
        assert s.tokens == 0

    def test_longer_content_more_tokens(self):
        short = PromptSection(content="Hi", name="s")
        long = PromptSection(content="This is a much longer piece of text.", name="l")
        assert long.tokens > short.tokens


# ---------------------------------------------------------------------------
# unwrap_multimodal_result
# ---------------------------------------------------------------------------


class TestUnwrapMultimodalResult:
    def test_none_returns_empty_string(self):
        assert unwrap_multimodal_result(None) == ""

    def test_string_returned_as_is(self):
        assert unwrap_multimodal_result("hello") == "hello"

    def test_non_string_non_dict_stringified(self):
        result = unwrap_multimodal_result(42)
        assert result == "42"

    def test_list_stringified(self):
        result = unwrap_multimodal_result(["a", "b"])
        assert "a" in result

    def test_multimodal_dict_with_text_only_routing(self):
        result = {
            "_multimodal": True,
            "content": [{"type": "text", "text": "screenshot"}],
            "text_summary": "A screenshot of the desktop",
        }
        with patch(
            "markbot.tools.computer_use.vision_routing.should_route_to_text_only",
            return_value=True,
        ):
            out = unwrap_multimodal_result(result)
            assert out == "A screenshot of the desktop"

    def test_multimodal_dict_with_vision_routing(self):
        content_blocks = [{"type": "text", "text": "screenshot"}]
        result = {
            "_multimodal": True,
            "content": content_blocks,
            "text_summary": "A screenshot of the desktop",
        }
        with patch(
            "markbot.tools.computer_use.vision_routing.should_route_to_text_only",
            return_value=False,
        ):
            out = unwrap_multimodal_result(result)
            assert out is content_blocks

    def test_multimodal_dict_no_content_falls_back_to_summary(self):
        result = {
            "_multimodal": True,
            "content": None,
            "text_summary": "fallback text",
        }
        with patch(
            "markbot.tools.computer_use.vision_routing.should_route_to_text_only",
            return_value=False,
        ):
            out = unwrap_multimodal_result(result)
            assert out == "fallback text"

    def test_multimodal_dict_no_content_no_summary(self):
        result = {
            "_multimodal": True,
            "content": None,
            "text_summary": "",
        }
        with patch(
            "markbot.tools.computer_use.vision_routing.should_route_to_text_only",
            return_value=False,
        ):
            out = unwrap_multimodal_result(result)
            assert out == ""

    def test_non_multimodal_dict_stringified(self):
        result = {"key": "value"}
        out = unwrap_multimodal_result(result)
        assert isinstance(out, str)
        assert "value" in out


# ---------------------------------------------------------------------------
# ContextBuilder (basic instantiation)
# ---------------------------------------------------------------------------


class TestContextBuilderInit:
    def test_default_token_budget(self):
        assert DEFAULT_SYSTEM_PROMPT_TOKEN_BUDGET == 16_000

    def test_init_with_workspace(self, tmp_path):
        builder = ContextBuilder(workspace=tmp_path)
        assert builder.workspace == tmp_path
        assert builder.system_prompt_token_budget == DEFAULT_SYSTEM_PROMPT_TOKEN_BUDGET

    def test_init_with_custom_budget(self, tmp_path):
        builder = ContextBuilder(workspace=tmp_path, system_prompt_token_budget=8000)
        assert builder.system_prompt_token_budget == 8000

    def test_build_system_prompt_returns_string(self, tmp_path):
        builder = ContextBuilder(workspace=tmp_path)
        prompt = builder.build_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_build_system_prompt_caches(self, tmp_path):
        builder = ContextBuilder(workspace=tmp_path)
        first = builder.build_system_prompt()
        second = builder.build_system_prompt()
        # Cached result should be identical
        assert first == second

    def test_build_system_prompt_with_skill_names(self, tmp_path):
        builder = ContextBuilder(workspace=tmp_path)
        prompt = builder.build_system_prompt(skill_names=["test-skill"])
        assert isinstance(prompt, str)
