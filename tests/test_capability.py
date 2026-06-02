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


class TestCapabilityTokenSerialization:
    def test_to_dict_roundtrip(self):
        token = CapabilityToken(
            allowed_tools=("read_file", "glob"),
            forbidden_tools=("exec", "write_file"),
            max_iterations=7,
            max_budget_usd=1.25,
            timeout_seconds=120.0,
            description="review",
        )
        cloned = CapabilityToken.from_dict(token.to_dict())
        assert cloned == token

    def test_from_dict_none_returns_read_only(self):
        token = CapabilityToken.from_dict(None)
        # None must fall back to the safe default profile.
        assert token == CapabilityToken.read_only()

    def test_from_dict_empty(self):
        token = CapabilityToken.from_dict({})
        assert token == CapabilityToken()

    def test_from_dict_snake_case(self):
        token = CapabilityToken.from_dict({
            "allowed_tools": ["read_file", "glob"],
            "forbidden_tools": ["exec"],
            "max_iterations": 3,
            "max_budget_usd": 0.5,
            "timeout_seconds": 60,
            "description": "test",
        })
        assert token.allowed_tools == ("read_file", "glob")
        assert token.forbidden_tools == ("exec",)
        assert token.max_iterations == 3
        assert token.max_budget_usd == 0.5
        assert token.timeout_seconds == 60.0
        assert token.description == "test"

    def test_from_dict_camel_case_aliases(self):
        token = CapabilityToken.from_dict({
            "allowedTools": ["read_file"],
            "forbiddenTools": ["exec", "write_file"],
            "maxIterations": 5,
            "maxBudgetUsd": 0.1,
            "timeoutSeconds": 30,
        })
        assert token.allowed_tools == ("read_file",)
        assert token.forbidden_tools == ("exec", "write_file")
        assert token.max_iterations == 5
        assert token.max_budget_usd == 0.1
        assert token.timeout_seconds == 30.0

    def test_from_dict_string_singleton_normalized_to_tuple(self):
        # Single string (e.g. "exec") should be wrapped into a 1-tuple.
        token = CapabilityToken.from_dict({"forbidden_tools": "exec"})
        assert token.forbidden_tools == ("exec",)

    def test_from_dict_preserves_unknown_keys_in_metadata(self):
        # Unknown keys (e.g. regional hint from a future caller) must not
        # be lost — they round-trip via metadata.
        token = CapabilityToken.from_dict({
            "forbidden_tools": ["exec"],
            "region": "us-east-1",
            "tenant_id": 42,
        })
        assert token.metadata == {"region": "us-east-1", "tenant_id": 42}
        assert token.forbidden_tools == ("exec",)

    def test_from_dict_preserves_explicit_metadata_field(self):
        token = CapabilityToken.from_dict({
            "metadata": {"audit": True},
            "region": "eu",
        })
        assert token.metadata["audit"] is True
        assert token.metadata["region"] == "eu"

    def test_from_dict_rejects_non_mapping(self):
        with pytest.raises(TypeError):
            CapabilityToken.from_dict("exec")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            CapabilityToken.from_dict(42)  # type: ignore[arg-type]

    def test_from_dict_rejects_non_string_list_entries(self):
        with pytest.raises(ValueError, match="must be strings"):
            CapabilityToken.from_dict({"forbidden_tools": [1, 2]})

    def test_from_dict_rejects_non_numeric_budget(self):
        with pytest.raises(ValueError, match="max_budget_usd"):
            CapabilityToken.from_dict({"max_budget_usd": "not-a-number"})

    def test_from_dict_rejects_non_integer_iterations(self):
        with pytest.raises(ValueError, match="max_iterations"):
            CapabilityToken.from_dict({"max_iterations": "many"})

    def test_from_dict_metadata_must_be_mapping(self):
        token = CapabilityToken.from_dict({"metadata": "not a dict"})
        assert token.metadata == {}

    def test_to_dict_lists_are_lists(self):
        token = CapabilityToken(
            allowed_tools=("read_file",),
            forbidden_tools=("exec",),
        )
        d = token.to_dict()
        # JSON-serializable: tuple -> list, not preserved on round-trip.
        assert isinstance(d["allowed_tools"], list)
        assert isinstance(d["forbidden_tools"], list)
