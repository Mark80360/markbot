"""Tool registry with permission integration.

Execution is delegated to :class:`~markbot.tools.pipeline.ToolPipeline`,
which owns the guarded five-stage flow (resolve → guards → pre-execute
→ execute → post-execute → finalise). The registry keeps the public
surface: registration, schema generation, and permission aggregation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from loguru import logger

from markbot.tools.base import BaseTool
from markbot.tools.pipeline import (
    AsyncGuard,
    AsyncPostHook,
    AsyncPreHook,
    EventSink,
    ToolPipeline,
    soft_disabled_error,
    unknown_tool_error,
)
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
        # Fingerprint of available tool names so service-gates re-evaluate
        # without forcing a full schema rebuild on every call.
        self._definitions_cache_key: tuple[str, ...] | None = None
        # Optional interactive approver. When set, ``ask`` decisions wait for
        # a real user yes/no instead of returning a text instruction to the
        # model. Signature: async (tool_name, params, context, reason) -> bool
        self._permission_approver: Callable | None = None
        # Soft-disabled names stay registered but are hidden from model schema.
        self._soft_disabled: set[str] = set()
        # Installed restriction filters (canonical name -> visible?).
        self._restriction_filters: list[Callable[[str], bool]] = []
        # Guarded execution pipeline (guards / hooks / timeout / audit).
        self.pipeline = ToolPipeline()

    # -- pipeline pass-throughs ------------------------------------------

    def add_guard(self, guard: AsyncGuard) -> Callable[[], None]:
        """Register a monotonic (deny-only) guard; returns its disposer."""
        return self.pipeline.add_guard(guard)

    def add_pre_execute_hook(self, hook: AsyncPreHook) -> Callable[[], None]:
        """Register a pre-execute hook (allow/deny/ask); returns disposer."""
        return self.pipeline.add_pre_execute_hook(hook)

    def add_post_execute_hook(self, hook: AsyncPostHook) -> Callable[[], None]:
        """Register a post-execute hook (observe/replace); returns disposer."""
        return self.pipeline.add_post_execute_hook(hook)

    def set_event_sink(self, sink: EventSink | None) -> None:
        """Attach an audit sink receiving ``(event_type, payload)`` pairs."""
        self.pipeline.set_event_sink(sink)

    def restrict(
        self,
        *,
        allowed: set[str] | None = None,
        denied: set[str] | None = None,
    ) -> Callable[[], None]:
        """Install a scoped execution+schema restriction; returns disposer.

        Restrictions stack: every installed filter must admit a tool before
        it becomes callable / visible. ``allowed`` whitelists names;
        ``denied`` blacklists them; a tool must pass both.

        Unknown *allowed* names fail loud (ValueError) at install time —
        a misspelled whitelist must never silently disable everything.
        Re-invoke ``restrict`` after deferred registrations (MCP) if the
        whitelist references late-arriving tools.

        Enforcement points: model-facing schema (definitions) and a
        monotonic pipeline guard, so a hidden name can never be executed
        even if the model hallucinates it.
        """
        known = set(self._tools) | set(self._aliases)
        # Defensive copies: later mutation of the caller's sets must not
        # silently change what this restriction enforces.
        allowed = set(allowed) if allowed else None
        denied = set(denied) if denied else None
        if allowed:
            unknown = {n for n in allowed if n not in known}
            if unknown:
                raise ValueError(
                    f"restrict(): unknown allowed tools: {sorted(unknown)}. "
                    f"Known: {sorted(known)}"
                )

        def guard(name: str, params: dict[str, Any], context: ToolContext) -> str | None:
            resolved = self.resolve_sanitised_name(name.strip())
            canonical = self._aliases.get(resolved, resolved)
            if denied and (canonical in denied or name in denied):
                return (
                    f"[TOOL_PERMISSION_DENIED] Tool '{name}' is forbidden in "
                    f"this context."
                )
            if allowed and canonical not in allowed:
                return (
                    f"[TOOL_PERMISSION_DENIED] Tool '{name}' is not permitted "
                    f"in this context. Allowed: {', '.join(sorted(allowed))}."
                )
            return None

        dispose_guard = self.pipeline.add_guard(guard)

        # Schema-level enforcement: restricted names leave the model-visible
        # surface entirely, mirroring the execution guard.
        def schema_filter(canonical_name: str) -> bool:
            if denied and canonical_name in denied:
                return False
            if allowed and canonical_name not in allowed:
                return False
            return True

        self._restriction_filters.append(schema_filter)
        self._invalidate_cache()

        def dispose() -> None:
            try:
                self._restriction_filters.remove(schema_filter)
            except ValueError:
                pass
            dispose_guard()
            self._invalidate_cache()

        return dispose

    def _passes_restrictions(self, canonical_name: str) -> bool:
        """True when no installed restriction excludes *canonical_name*."""
        return all(filt(canonical_name) for filt in self._restriction_filters)

    def _invalidate_cache(self) -> None:
        self._definitions_cache = None
        self._definitions_cache_key = None

    def invalidate_definitions_cache(self) -> None:
        """Public cache bust — e.g. after deferred tool-surface mutations."""
        self._invalidate_cache()

    def set_soft_disabled(self, names: set[str] | frozenset[str] | list[str]) -> None:
        """Hide *names* from model-facing schema without unregistering."""
        new = {str(n) for n in names}
        if new != self._soft_disabled:
            self._soft_disabled = new
            self._invalidate_cache()

    def get_soft_disabled(self) -> set[str]:
        return set(self._soft_disabled)

    def _is_tool_available(self, tool: BaseTool) -> bool:
        """True when the tool should appear in model-facing schema."""
        name = getattr(getattr(tool, "definition", None), "name", None) or ""
        if name and name in self._soft_disabled:
            return False
        if name and not self._passes_restrictions(name):
            return False
        try:
            if hasattr(tool, "is_available"):
                return bool(tool.is_available())
            return bool(getattr(tool, "is_enabled", True))
        except Exception:
            return False

    def _available_tools(self) -> list[BaseTool]:
        return [t for t in self._tools.values() if self._is_tool_available(t)]

    def _available_names_key(self) -> tuple[str, ...]:
        return tuple(sorted(t.definition.name for t in self._available_tools()))

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
        """Get definitions for tools currently available to the model.

        Unavailable (service-gated / disabled) tools are omitted so they do
        not inflate every API call's tool schema footprint.
        """
        return [t.definition for t in self._available_tools()]

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions in OpenAI format (cached by available-name set).

        Cache key is the sorted tuple of available tool names. When a gate
        flips mid-session, the next call rebuilds schema without requiring an
        explicit invalidate.
        """
        key = self._available_names_key()
        if self._definitions_cache is None or self._definitions_cache_key != key:
            self._definitions_cache = [d.to_openai_schema() for d in self.definitions]
            self._definitions_cache_key = key
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

    def set_permission_approver(self, approver: Callable | None) -> None:
        """Set interactive permission approver for ``ask`` decisions.

        The approver should be an async callable:
        ``(tool_name: str, params: dict, context: ToolContext, reason: str) -> bool``.
        Returning True allows the tool; False denies it.
        """
        self._permission_approver = approver

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
        *,
        extra_guards: list[Callable[[str, dict[str, Any], ToolContext], Any]] | None = None,
    ) -> Any:
        """
        Execute a tool through the guarded pipeline.

        This is the main entry point for tool execution.

        *extra_guards* are call-scoped deny-only guards (sync or async,
        ``(name, params, context) -> denial_text | None``). Their denials
        are returned as :class:`~markbot.tools.pipeline.PreblockedResult`
        so turn runners can distinguish pre-blocks from real failures.
        """
        tool = self.get(name)
        if not tool:
            return unknown_tool_error(name, self.tool_names)

        # Soft-disabled tools stay registered for hot re-enable, but must not
        # execute — defence-in-depth when the model hallucinates a hidden name.
        tool_name = getattr(getattr(tool, "definition", None), "name", name) or name
        if tool_name in self._soft_disabled:
            available = [t.definition.name for t in self._available_tools()]
            return soft_disabled_error(tool_name, available)

        # Create default context if not provided
        if context is None:
            context = ToolContext(
                session_id="default",
                workspace=".",
                permission_mode=PermissionMode.DEFAULT,
                tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
                is_non_interactive=True,
            )

        decision = await self.check_permission(tool, params, context)
        return await self.pipeline.run(
            tool,
            name,
            params,
            context,
            base_decision=decision,
            approver=self._permission_approver,
            extra_guards=extra_guards,
        )

    def get_definitions_for_provider(self, provider: str = "openai") -> list[dict[str, Any]]:
        """Get tool definitions for specific provider format."""
        tools = [t for t in self._tools.values() if self._is_tool_available(t)]

        if provider == "anthropic":
            return [t.definition.to_anthropic_schema() for t in tools]
        else:
            return [t.definition.to_openai_schema() for t in tools]

    def register_gated(
        self,
        tool: BaseTool,
        *,
        available_when: Callable[[], bool] | None = None,
    ) -> bool:
        """Register *tool* only when the optional gate returns True.

        Returns whether the tool was registered. Prefer this over registering
        unavailable tools and relying on soft-disable later — gated tools never
        enter the schema and never inflate API cost.
        """
        if available_when is not None:
            try:
                if not available_when():
                    logger.debug(
                        "Skipped gated tool {} (available_when=False)",
                        getattr(getattr(tool, "definition", None), "name", tool),
                    )
                    return False
            except Exception as e:
                logger.debug("Gate check failed, skipping tool: {}", e)
                return False
        elif not self._is_tool_available(tool):
            logger.debug(
                "Skipped gated tool {} (is_available=False)",
                getattr(getattr(tool, "definition", None), "name", tool),
            )
            return False
        self.register(tool)
        return True

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
