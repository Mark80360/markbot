"""Tool registry with permission integration.

Refactored to use new core types inspired by MarkBot.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from loguru import logger

from markbot.tools.base import BaseTool
from markbot.types.permission import PermissionDecision, PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext, ToolDefinition, _sanitize_tool_name


class ToolRegistry:
    """
    Enhanced tool registry with permission system.

    Inspired by MarkBot's tool system.
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._aliases: dict[str, str] = {}  # alias -> name
        # Reverse map: sanitised wire-name -> original name. Populated on
        # register() so that a model returning the sanitised name (the form
        # we send on the wire) can be resolved back to the in-process tool.
        self._sanitised: dict[str, str] = {}
        self._permission_handlers: list[
            Callable[[BaseTool, dict, ToolContext], PermissionDecision]
        ] = []
        self._definitions_cache: list[dict[str, Any]] | None = None

    def _invalidate_cache(self) -> None:
        self._definitions_cache = None

    def register(self, tool: BaseTool) -> None:
        """Register a tool."""
        name = tool.definition.name
        self._tools[name] = tool

        # Register aliases
        for alias in tool.definition.aliases:
            self._aliases[alias] = name

        # Register sanitised name -> original name for round-tripping
        # provider-returned tool_call.function.name back to our registry.
        sanitised = _sanitize_tool_name(name)
        if sanitised and sanitised != name:
            # Only add the reverse mapping if the sanitised form actually
            # differs (i.e. the original name had a non-ASCII char) so that
            # we don't shadow unrelated tools when the name was already safe.
            self._sanitised.setdefault(sanitised, name)

        self._invalidate_cache()
        logger.debug("Registered tool: {}", name)

    def unregister(self, name: str) -> None:
        """Unregister a tool."""
        name = name.strip()
        tool = self._tools.pop(name, None)

        if tool:
            # Remove aliases
            for alias in list(self._aliases.keys()):
                if self._aliases[alias] == name:
                    del self._aliases[alias]
            # Remove sanitised reverse mapping
            for sanitised, original in list(self._sanitised.items()):
                if original == name:
                    del self._sanitised[sanitised]

            self._invalidate_cache()

    def get(self, name: str) -> Optional[BaseTool]:
        """Get a tool by name or alias."""
        name = name.strip()

        # Check direct name
        if name in self._tools:
            return self._tools[name]

        # Check alias
        if name in self._aliases:
            return self._tools.get(self._aliases[name])

        # Check sanitised name (provider may echo the wire form)
        resolved = self._sanitised.get(name)
        if resolved is not None:
            return self._tools.get(resolved)

        return None

    def has(self, name: str) -> bool:
        """Check if tool exists."""
        return self.get(name) is not None

    @property
    def definitions(self) -> list[ToolDefinition]:
        """Get all tool definitions."""
        return [t.definition for t in self._tools.values() if t.is_enabled]

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions in OpenAI format (cached)."""
        if self._definitions_cache is None:
            self._definitions_cache = [d.to_openai_schema() for d in self.definitions]
        return self._definitions_cache

    @property
    def tool_names(self) -> list[str]:
        """Get all tool names."""
        return list(self._tools.keys())

    def resolve_sanitised_name(self, name: str) -> str:
        """Return the in-process name for a wire (sanitised) name.

        If *name* is not a known sanitised alias, it is returned unchanged.
        """
        return self._sanitised.get(name, name)

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
                logger.error("Permission handler failed: {}", e)

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
            available = ", ".join(sorted(self.tool_names))
            return (
                f"Error: Tool '{name}' does not exist. "
                f"This may be a hallucinated tool name. "
                f"Available tools: {available}. "
                f"Use only the tools listed above."
            )

        # Create default context if not provided
        if context is None:
            context = ToolContext(
                session_id="default",
                workspace=".",
                permission_mode=PermissionMode.DEFAULT,
                tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
                is_non_interactive=True,
            )

        # Check permission
        decision = await self.check_permission(tool, params, context)

        if decision.behavior == "deny":
            return f"Error: Tool '{name}' execution denied."

        if decision.behavior == "ask":
            reason = f" Reason: {decision.reason}" if decision.reason else ""
            return (
                f"Permission required: Tool '{name}' was not executed.{reason} "
                "Ask the user for explicit approval, or switch to a mode/profile "
                "that permits this tool."
            )

        # Update params if modified by permission check
        if decision.updated_input:
            params = decision.updated_input

        # Execute
        try:
            logger.info("Executing tool: {}", name)
            result = await tool.execute(params, context)
            return result
        except Exception as e:
            logger.error("Tool execution failed: {}", e)
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
