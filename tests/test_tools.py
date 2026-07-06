"""Tests for markbot.tools module (base, registry)."""

import pytest

from markbot.tools.base import BaseTool, _is_under, _resolve_path
from markbot.tools.registry import ToolRegistry
from markbot.types.permission import PermissionDecision, PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter


class ConcreteTool(BaseTool):
    """Concrete implementation for testing."""

    @property
    def definition(self):
        return ToolDefinition(
            name="test_tool",
            description="A test tool",
            parameters=[
                ToolParameter(name="input", type="string", description="Input text"),
                ToolParameter(name="count", type="integer", description="Count", required=False, default=1),
            ],
            is_read_only=True,
        )

    async def execute(self, params, context):
        return f"executed with {params}"


class DestructiveTool(BaseTool):
    """Destructive tool for permission testing."""

    @property
    def definition(self):
        return ToolDefinition(
            name="delete_file",
            description="Delete a file",
            parameters=[
                ToolParameter(name="path", type="string", description="File path"),
            ],
            is_read_only=False,
            is_destructive=True,
        )

    async def execute(self, params, context):
        return "deleted"


class TestResolvePath:
    def test_absolute_path(self, tmp_path):
        result = _resolve_path(str(tmp_path))
        assert result == tmp_path.resolve()

    def test_relative_path_with_workspace(self, tmp_path):
        result = _resolve_path("subdir", workspace=tmp_path)
        assert result == (tmp_path / "subdir").resolve()

    def test_path_outside_allowed_dir(self, tmp_path):
        with pytest.raises(PermissionError):
            _resolve_path("/etc/passwd", allowed_dir=tmp_path)


class TestIsUnder:
    def test_path_under_dir(self, tmp_path):
        child = tmp_path / "sub" / "file.txt"
        assert _is_under(child, tmp_path) is True

    def test_path_not_under_dir(self, tmp_path):
        other = tmp_path.parent / "other"
        assert _is_under(other, tmp_path) is False


class TestBaseTool:
    def test_definition(self):
        tool = ConcreteTool()
        assert tool.definition.name == "test_tool"
        assert tool.is_enabled is True

    def test_is_read_only(self):
        tool = ConcreteTool()
        assert tool.is_read_only({}) is True

    def test_is_destructive(self):
        tool = ConcreteTool()
        assert tool.is_destructive({}) is False

    def test_cast_params(self):
        tool = ConcreteTool()
        result = tool.cast_params({"input": "hello", "count": "5"})
        assert result["input"] == "hello"

    def test_get_activity_description(self):
        tool = ConcreteTool()
        desc = tool.get_activity_description({"input": "test"})
        assert "test_tool" in desc

    @pytest.mark.asyncio
    async def test_execute(self):
        tool = ConcreteTool()
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        )
        result = await tool.execute({"input": "hello"}, ctx)
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_permission_read_only_allow(self):
        tool = ConcreteTool()
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        )
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "allow"

    @pytest.mark.asyncio
    async def test_permission_always_deny(self):
        tool = DestructiveTool()
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(
                mode=PermissionMode.DEFAULT,
                always_deny={"delete_file"},
            ),
        )
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "deny"

    @pytest.mark.asyncio
    async def test_permission_always_allow(self):
        tool = DestructiveTool()
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(
                mode=PermissionMode.DEFAULT,
                always_allow={"delete_file"},
            ),
        )
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "allow"

    @pytest.mark.asyncio
    async def test_permission_auto_mode(self):
        tool = DestructiveTool()
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.AUTO,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.AUTO),
        )
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "allow"

    @pytest.mark.asyncio
    async def test_permission_plan_mode_blocks_destructive(self):
        tool = DestructiveTool()
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.PLAN,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.PLAN),
        )
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "deny"
    @pytest.mark.asyncio
    async def test_permission_default_ask(self):
        tool = DestructiveTool()
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        )
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "ask"
    @pytest.mark.asyncio
    async def test_permission_bypass_mode(self):
        tool = DestructiveTool()
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.BYPASS,
            tool_permission_context=ToolPermissionContext(
                mode=PermissionMode.BYPASS,
                is_bypass_available=True,
            ),
        )
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "allow"


    @pytest.mark.asyncio
    async def test_permission_always_deny_overrides_read_only(self):
        """always_deny must take priority even for read-only tools."""
        tool = ConcreteTool()  # is_read_only=True
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(
                mode=PermissionMode.DEFAULT,
                always_deny={"test_tool"},
            ),
        )
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "deny"


    @pytest.mark.asyncio
    async def test_permission_always_allow_overrides_read_only(self):
        """always_allow is redundant for read-only tools but should still work."""
        tool = ConcreteTool()  # is_read_only=True
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(
                mode=PermissionMode.DEFAULT,
                always_allow={"test_tool"},
            ),
        )
        decision = await tool.check_permission({}, ctx)
        assert decision.behavior == "allow"

class TestRegistryPermissionGate:
    """The registry must turn ``ask`` into a hard stop, not a silent allow."""

    @pytest.mark.asyncio
    async def test_ask_blocks_execution(self):
        reg = ToolRegistry()
        tool = DestructiveTool()
        reg.register(tool)
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        )
        result = await reg.execute("delete_file", {"path": "x"}, context=ctx)
        assert "Permission required" in result

    @pytest.mark.asyncio
    async def test_default_context_blocks_destructive(self):
        """When no context is provided the registry must use DEFAULT, not AUTO."""
        reg = ToolRegistry()
        reg.register(DestructiveTool())
        result = await reg.execute("delete_file", {"path": "x"})
        assert "Permission required" in result


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        tool = ConcreteTool()
        reg.register(tool)
        assert reg.get("test_tool") is tool

    def test_get_nonexistent(self):
        reg = ToolRegistry()
        assert reg.get("no_such_tool") is None

    def test_has(self):
        reg = ToolRegistry()
        tool = ConcreteTool()
        reg.register(tool)
        assert reg.has("test_tool") is True
        assert reg.has("no_such_tool") is False

    def test_unregister(self):
        reg = ToolRegistry()
        tool = ConcreteTool()
        reg.register(tool)
        reg.unregister("test_tool")
        assert reg.has("test_tool") is False

    def test_definitions(self):
        reg = ToolRegistry()
        reg.register(ConcreteTool())
        defs = reg.definitions
        assert len(defs) == 1
        assert defs[0].name == "test_tool"

    def test_get_definitions_openai_format(self):
        reg = ToolRegistry()
        reg.register(ConcreteTool())
        schemas = reg.get_definitions()
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"

    def test_tool_names(self):
        reg = ToolRegistry()
        reg.register(ConcreteTool())
        assert "test_tool" in reg.tool_names

    def test_alias_support(self):
        class AliasedTool(BaseTool):
            @property
            def definition(self):
                return ToolDefinition(
                    name="read_file", description="Read", parameters=[],
                    aliases=["rf", "cat"],
                )

            async def execute(self, params, context):
                return "ok"

        reg = ToolRegistry()
        reg.register(AliasedTool())
        assert reg.get("rf") is not None
        assert reg.get("cat") is not None
        assert reg.get("read_file") is not None

    def test_unregister_removes_aliases(self):
        class AliasedTool(BaseTool):
            @property
            def definition(self):
                return ToolDefinition(
                    name="read_file", description="Read", parameters=[],
                    aliases=["rf"],
                )

            async def execute(self, params, context):
                return "ok"

        reg = ToolRegistry()
        reg.register(AliasedTool())
        reg.unregister("read_file")
        assert reg.get("rf") is None

    @pytest.mark.asyncio
    async def test_check_permission_deny_wins(self):
        reg = ToolRegistry()
        tool = ConcreteTool()
        reg.register(tool)

        def deny_handler(t, p, c):
            return PermissionDecision(behavior="deny", reason="blocked")

        reg.add_permission_handler(deny_handler)

        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        )
        decision = await reg.check_permission(tool, {}, ctx)
        assert decision.behavior == "deny"

    @pytest.mark.asyncio
    async def test_check_permission_ask_overrides_allow(self):
        reg = ToolRegistry()
        tool = ConcreteTool()
        reg.register(tool)

        def ask_handler(t, p, c):
            return PermissionDecision(behavior="ask")

        reg.add_permission_handler(ask_handler)

        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        )
        decision = await reg.check_permission(tool, {}, ctx)
        assert decision.behavior == "ask"

    def test_definitions_cache_invalidated(self):
        reg = ToolRegistry()
        reg.register(ConcreteTool())
        _ = reg.get_definitions()
        reg.register(DestructiveTool())
        schemas = reg.get_definitions()
        assert len(schemas) == 2
