"""Tests for markbot.agent.subagent.capability — CapabilityToken."""

import pytest

from markbot.agent.subagent.capability import CapabilityToken


class TestCapabilityToken:
    def test_default_values(self):
        token = CapabilityToken()
        assert token.allowed_tools == ()
        assert token.forbidden_tools == ()
        assert token.max_iterations == 15
        assert token.max_budget_usd is None
        assert token.timeout_seconds is None
        assert token.description == ""
        assert token.metadata == {}

    def test_custom_values(self):
        token = CapabilityToken(
            allowed_tools=("read_file", "glob"),
            forbidden_tools=("exec",),
            max_iterations=5,
            max_budget_usd=1.0,
            timeout_seconds=60,
            description="test",
            metadata={"key": "val"},
        )
        assert token.allowed_tools == ("read_file", "glob")
        assert token.forbidden_tools == ("exec",)
        assert token.max_iterations == 5
        assert token.max_budget_usd == 1.0
        assert token.timeout_seconds == 60
        assert token.description == "test"
        assert token.metadata == {"key": "val"}

    def test_frozen(self):
        token = CapabilityToken()
        with pytest.raises(AttributeError):
            token.max_iterations = 10

    def test_read_only_factory(self):
        token = CapabilityToken.read_only()
        assert "read_file" in token.allowed_tools
        assert "glob" in token.allowed_tools
        assert "grep" in token.allowed_tools
        assert "web_search" in token.allowed_tools
        assert "exec" in token.forbidden_tools
        assert "write_file" in token.forbidden_tools
        assert token.max_budget_usd == 0.5
        assert token.timeout_seconds == 300

    def test_read_only_custom_description(self):
        token = CapabilityToken.read_only(description="Custom desc")
        assert token.description == "Custom desc"

    def test_allows_empty_allowed_tools(self):
        """Empty allowed_tools means inherit/no restriction."""
        token = CapabilityToken()
        assert token.allows("any_tool") is True

    def test_allows_specific_tool(self):
        token = CapabilityToken(allowed_tools=("read_file", "glob"))
        assert token.allows("read_file") is True
        assert token.allows("glob") is True
        assert token.allows("exec") is False

    def test_allows_forbidden_overrides_allowed(self):
        token = CapabilityToken(
            allowed_tools=("read_file", "exec"),
            forbidden_tools=("exec",),
        )
        assert token.allows("read_file") is True
        assert token.allows("exec") is False

    def test_allows_forbidden_without_allowed(self):
        token = CapabilityToken(forbidden_tools=("exec",))
        assert token.allows("read_file") is True
        assert token.allows("exec") is False

    def test_read_only_allows(self):
        token = CapabilityToken.read_only()
        assert token.allows("read_file") is True
        assert token.allows("glob") is True
        assert token.allows("exec") is False
        assert token.allows("write_file") is False
        assert token.allows("spawn") is False

    def test_equality(self):
        t1 = CapabilityToken(max_iterations=5)
        t2 = CapabilityToken(max_iterations=5)
        assert t1 == t2

    def test_inequality(self):
        t1 = CapabilityToken(max_iterations=5)
        t2 = CapabilityToken(max_iterations=10)
        assert t1 != t2
