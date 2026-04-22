"""Tool registry with permission integration.

Refactored to use new core types inspired by MarkBot.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from loguru import logger

from markbot.tools.base import BaseTool
from markbot.types.permission import PermissionDecision, PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext, ToolDefinition


class ToolRegistry:
    """
    Enhanced tool registry with permission system.

    Inspired by MarkBot's tool system.
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._aliases: dict[str, str] = {}  # alias -> name
        self._permission_handlers: list[
            Callable[[BaseTool, dict, ToolContext], PermissionDecision]
        ] = []

    def register(self, tool: BaseTool) -> None:
        """Register a tool."""
        name = tool.definition.name
        self._tools[name] = tool

        # Register aliases
        for alias in tool.definition.aliases:
            self._aliases[alias] = name

        logger.debug(f"Registered tool: {name}")

    def unregister(self, name: str) -> None:
        """Unregister a tool."""
        name = name.strip()
        tool = self._tools.pop(name, None)

        if tool:
            # Remove aliases
            for alias in list(self._aliases.keys()):
                if self._aliases[alias] == name:
                    del self._aliases[alias]

    def get(self, name: str) -> Optional[BaseTool]:
        """Get a tool by name or alias."""
        name = name.strip()

        # Check direct name
        if name in self._tools:
            return self._tools[name]

        # Check alias
        if name in self._aliases:
            return self._tools.get(self._aliases[name])

        return None

    def has(self, name: str) -> bool:
        """Check if tool exists."""
        return self.get(name) is not None

    @property
    def definitions(self) -> list[ToolDefinition]:
        """Get all tool definitions."""
        return [t.definition for t in self._tools.values() if t.is_enabled]

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions in OpenAI format.

        This is the legacy method used by the agent loop.
        """
        return [d.to_openai_schema() for d in self.definitions]

    @property
    def tool_names(self) -> list[str]:
        """Get all tool names."""
        return list(self._tools.keys())

    def add_permission_handler(
        self,
        handler: Callable[[BaseTool, dict, ToolContext], PermissionDecision],
    ) -> None:
        """Add a permission handler."""
        self._permission_handlers.append(handler)

    async def check_permission(
        self,
        tool: BaseTool,
        params: dict[str, Any],
        context: ToolContext,
    ) -> PermissionDecision:
        """
        Check tool permission with all registered handlers.

        Returns the most restrictive decision.
        """
        # First check tool's own permission
        decision = await tool.check_permission(params, context)

        if decision.behavior == "deny":
            return decision

        # Run through all handlers
        for handler in self._permission_handlers:
            try:
                result = handler(tool, params, context)
                if isinstance(result, PermissionDecision):
                    handler_decision = result
                else:
                    handler_decision = await result

                # Most restrictive wins
                if handler_decision.behavior == "deny":
                    return handler_decision
                elif handler_decision.behavior == "ask" and decision.behavior == "allow":
                    decision = handler_decision

            except Exception as e:
                logger.error(f"Permission handler failed: {e}")

        return decision

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> Any:
        """
        Execute a tool with permission checking.

        This is the main entry point for tool execution.
        """
        tool = self.get(name)
        if not tool:
            available = ", ".join(self.tool_names[:10])
            return f"Error: Tool '{name}' not found. Available: {available}..."

        # Create default context if not provided
        if context is None:
            context = ToolContext(
                session_id="default",
                workspace=".",
                permission_mode=PermissionMode.AUTO,
                tool_permission_context=ToolPermissionContext(mode=PermissionMode.AUTO),
                is_non_interactive=True,
            )

        # Check permission
        decision = await self.check_permission(tool, params, context)

        if decision.behavior == "deny":
            return f"Error: Tool '{name}' execution denied."

        if decision.behavior == "ask" and not context.is_non_interactive:
            # In interactive mode, we would show a permission dialog
            # For now, we'll allow it (the UI layer should handle this)
            pass

        # Update params if modified by permission check
        if decision.updated_input:
            params = decision.updated_input

        # Execute
        try:
            logger.info(f"Executing tool: {name}")
            result = await tool.execute(params, context)
            return result
        except Exception as e:
            logger.error(f"Tool execution failed: {e}")
            return f"Error executing {name}: {str(e)}"

    def get_definitions_for_provider(self, provider: str = "openai") -> list[dict[str, Any]]:
        """Get tool definitions for specific provider format."""
        tools = [t for t in self._tools.values() if t.is_enabled]

        if provider == "anthropic":
            return [t.definition.to_anthropic_schema() for t in tools]
        else:
            return [t.definition.to_openai_schema() for t in tools]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
