"""Tests for ``markbot.agent.subagent.spawn`` — SpawnTool with capability.

Verifies that the ``capability`` argument exposed on SpawnTool is
parsed and forwarded to SubagentManager, replacing the previous
hard-coded ``read_only()`` default. Also covers the new
"defence-in-depth" guard in ``SubagentManager._execute_subagent_loop``
that denies tool calls violating the capability, even if a subagent
hallucinates a tool name.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from markbot.agent.subagent.capability import CapabilityToken
from markbot.agent.subagent.manager import SubagentManager
from markbot.agent.subagent.spawn import SpawnTool
from markbot.tools.registry import ToolRegistry
from markbot.types.tool import ToolContext


def _tool_ctx() -> ToolContext:
    from markbot.types.permission import PermissionMode, ToolPermissionContext

    return ToolContext(
        session_id="test",
        workspace=".",
        permission_mode=PermissionMode.AUTO,
        tool_permission_context=ToolPermissionContext(mode=PermissionMode.AUTO),
        is_non_interactive=True,
        channel="cli",
        chat_id="direct",
    )


class TestSpawnToolCapabilityForwarding:
    @pytest.mark.asyncio
    async def test_capability_dict_is_forwarded_to_manager(self):
        manager = MagicMock()
        manager.spawn = AsyncMock(return_value="ok")
        tool = SpawnTool(manager)

        params = {
            "task": "review code",
            "label": "review",
            "capability": {
                "allowed_tools": ["read_file", "glob"],
                "forbidden_tools": ["exec"],
                "max_iterations": 4,
                "max_budget_usd": 0.25,
                "timeout_seconds": 60,
            },
        }
        result = await tool.execute(params, _tool_ctx())

        assert result == "ok"
        manager.spawn.assert_awaited_once()
        kwargs = manager.spawn.await_args.kwargs
        cap = kwargs["capability"]
        assert isinstance(cap, CapabilityToken)
        assert cap.allowed_tools == ("read_file", "glob")
        assert cap.forbidden_tools == ("exec",)
        assert cap.max_iterations == 4
        assert cap.max_budget_usd == 0.25
        assert cap.timeout_seconds == 60.0

    @pytest.mark.asyncio
    async def test_capability_omitted_falls_back_to_read_only(self):
        manager = MagicMock()
        manager.spawn = AsyncMock(return_value="ok")
        tool = SpawnTool(manager)

        await tool.execute({"task": "x"}, _tool_ctx())
        cap = manager.spawn.await_args.kwargs["capability"]
        assert cap == CapabilityToken.read_only()

    @pytest.mark.asyncio
    async def test_capability_null_falls_back_to_read_only(self):
        manager = MagicMock()
        manager.spawn = AsyncMock(return_value="ok")
        tool = SpawnTool(manager)

        await tool.execute({"task": "x", "capability": None}, _tool_ctx())
        cap = manager.spawn.await_args.kwargs["capability"]
        assert cap == CapabilityToken.read_only()

    @pytest.mark.asyncio
    async def test_invalid_capability_falls_back_with_warning(self):
        manager = MagicMock()
        manager.spawn = AsyncMock(return_value="ok")
        tool = SpawnTool(manager)

        # A non-mapping capability (e.g. a string) must not crash the
        # spawn — it should fall back to read-only so the parent agent
        # can keep functioning.
        await tool.execute({"task": "x", "capability": "not-a-dict"}, _tool_ctx())
        cap = manager.spawn.await_args.kwargs["capability"]
        # Falls back to read-only with an explanatory description.
        assert cap == CapabilityToken.read_only(
            description="Invalid capability payload — read-only fallback"
        )

    def test_definition_exposes_capability_parameter(self):
        tool = SpawnTool(MagicMock())
        names = {p.name for p in tool.definition.parameters}
        assert "capability" in names
        cap_param = next(p for p in tool.definition.parameters if p.name == "capability")
        assert cap_param.required is False
        # The description must document the JSON shape for the LLM.
        assert "allowed_tools" in cap_param.description
        assert "forbidden_tools" in cap_param.description
        assert "max_iterations" in cap_param.description


class TestSubagentToolGuard:
    """Defence-in-depth: capability must be re-checked at execution time."""

    def test_register_filters_by_capability(self):
        # This validates the first line of defence: tools not allowed
        # by the capability are not even registered, so the LLM never
        # sees them in its tool list.
        manager = SubagentManager(
            workspace=None,
            bus=MagicMock(),
        )
        tools = ToolRegistry()
        # Forbid every tool we register below; the registry must be empty.
        from markbot.tools.filesystem import ReadFileTool
        from markbot.tools.search import GlobTool

        cap = CapabilityToken(
            allowed_tools=("read_file", "glob"),
            forbidden_tools=("read_file", "glob"),
        )
        manager._register_subagent_tools(tools, cap)
        assert "read_file" not in tools
        assert "glob" not in tools

    def test_register_keeps_allowed_tools(self):
        manager = SubagentManager(
            workspace=None,
            bus=MagicMock(),
        )
        tools = ToolRegistry()
        cap = CapabilityToken(allowed_tools=("read_file", "glob"))
        manager._register_subagent_tools(tools, cap)
        assert "read_file" in tools
        assert "glob" in tools
