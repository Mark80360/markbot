"""Base class for agent tools.

Refactored to use new core types inspired by MarkBot.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

from markbot.types.permission import PermissionDecision, PermissionMode
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter


def _resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
) -> Path:
    """Resolve path against workspace (if relative) and enforce directory restriction."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if allowed_dir:
        all_dirs = [allowed_dir] + (extra_allowed_dirs or [])
        if not any(_is_under(resolved, d) for d in all_dirs):
            raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


def _is_under(path: Path, directory: Path) -> bool:
    """Check if path is within directory."""
    try:
        path.relative_to(directory.resolve())
        return True
    except ValueError:
        return False


class BaseTool(ABC):
    """
    Abstract base class for agent tools.

    Refactored to align with MarkBot's tool system.
    Tools are capabilities that the agent can use to interact with
    the environment, such as reading files, executing commands, etc.
    """

    _TYPE_MAP = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Tool definition including name, description, and parameters."""
        pass

    @property
    def is_enabled(self) -> bool:
        """Check if tool is enabled. Defaults to True."""
        return True

    def available_when(self) -> bool:
        """Service-gate: return False to hide the tool from model schema.

        Use this for tools that need external credentials / binaries.
        When False, the tool is not exposed via ``ToolRegistry.definitions``
        so it does not inflate every API call's tool footprint.
        Defaults to True. Prefer overriding this over permanently registering
        unavailable tools.
        """
        return True

    def is_available(self) -> bool:
        """Combined enable + service gate check."""
        try:
            return bool(self.is_enabled) and bool(self.available_when())
        except Exception:
            return False

    def is_read_only(self, params: dict[str, Any]) -> bool:
        """Check if tool operation is read-only. Defaults to False."""
        return self.definition.is_read_only

    def is_destructive(self, params: dict[str, Any]) -> bool:
        """Check if tool performs irreversible operations. Defaults to False."""
        return self.definition.is_destructive

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        """
        Check if tool has permission to run.

        Returns ALLOW for read-only tools, ASK for others.
        Override for custom permission logic.
        """
        # Explicit deny/allow lists take priority over read-only shortcut
        tool_name = self.definition.name
        ctx = context.tool_permission_context

        if tool_name in ctx.always_deny:
            return PermissionDecision(behavior="deny", reason="Tool in always-deny list")

        if tool_name in ctx.always_allow:
            return PermissionDecision(behavior="allow", reason="Tool in always-allow list")

        # Read-only tools are allowed by default
        if self.is_read_only(params):
            return PermissionDecision(behavior="allow")

        if tool_name in ctx.always_ask:
            return PermissionDecision(behavior="ask")

        if context.permission_mode == PermissionMode.PLAN:
            return PermissionDecision(
                behavior="deny",
                reason="Plan mode only permits read-only tools",
            )

        # AUTO mode: allow all tools. This mode should only be selected by an
        # explicit user/profile decision; DEFAULT remains confirmation-first.
        if context.permission_mode == PermissionMode.AUTO:
            return PermissionDecision(behavior="allow", reason="AUTO mode")

        # Check permission mode
        if context.permission_mode == PermissionMode.ACCEPT_EDITS:
            # In accept_edits mode, allow file operations
            if not self.is_destructive(params):
                return PermissionDecision(behavior="allow")

        if context.permission_mode == PermissionMode.BYPASS:
            if ctx.is_bypass_available:
                return PermissionDecision(behavior="allow", reason="Bypass mode active")

        return PermissionDecision(behavior="ask")

    @abstractmethod
    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        """
        Execute the tool with given parameters.

        Args:
            params: Tool-specific parameters (already validated and cast)
            context: Execution context

        Returns:
            Result of the tool execution (string or list of content blocks)
        """
        pass

    def get_activity_description(self, params: dict[str, Any]) -> Optional[str]:
        """Get human-readable activity description for display."""
        return f"Using {self.definition.name}"

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply safe schema-driven casts before validation."""
        result = {}
        for param in self.definition.parameters:
            if param.name in params:
                result[param.name] = self._cast_value(params[param.name], param.type)
            elif param.default is not None:
                result[param.name] = param.default
        return result

    def _cast_value(self, val: Any, target_type: str) -> Any:
        """Cast a single value to target type."""
        if val is None:
            return None

        if target_type == "string":
            return str(val)

        if target_type == "integer":
            if isinstance(val, bool):
                return 1 if val else 0
            if isinstance(val, int):
                return val
            if isinstance(val, str):
                try:
                    return int(val)
                except ValueError:
                    return val
            return val

        if target_type == "number":
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return val
            if isinstance(val, str):
                try:
                    return float(val)
                except ValueError:
                    return val
            return val

        if target_type == "boolean":
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                val_lower = val.lower()
                if val_lower in ("true", "1", "yes"):
                    return True
                if val_lower in ("false", "0", "no"):
                    return False
            return val

        if target_type == "array" and isinstance(val, list):
            return val

        if target_type == "object" and isinstance(val, dict):
            return val

        return val

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate tool parameters. Returns error list (empty if valid)."""
        errors = []

        # Check required parameters
        for param in self.definition.parameters:
            if param.required and param.name not in params:
                errors.append(f"Missing required parameter: {param.name}")

        # Check types
        for param in self.definition.parameters:
            if param.name in params:
                val = params[param.name]
                if val is None and not param.required:
                    continue

                type_errors = self._validate_type(val, param.type, param.name)
                errors.extend(type_errors)

                # Check enum
                if param.enum and val not in param.enum:
                    errors.append(f"{param.name} must be one of {param.enum}")

        return errors

    def _validate_type(self, val: Any, expected_type: str, name: str) -> list[str]:
        """Validate a value's type."""
        if val is None:
            return []

        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }

        expected = type_map.get(expected_type)
        if expected is None:
            return []

        if expected_type == "integer":
            if not isinstance(val, int) or isinstance(val, bool):
                return [f"{name} should be integer, got {type(val).__name__}"]
        elif expected_type == "number":
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                return [f"{name} should be number, got {type(val).__name__}"]
        elif not isinstance(val, expected):
            return [f"{name} should be {expected_type}, got {type(val).__name__}"]

        return []

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function schema format."""
        return self.definition.to_openai_schema()

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Convert to Anthropic tool schema format."""
        return self.definition.to_anthropic_schema()


class Tool(BaseTool):
    """
    Backward-compatible tool base class.

    This class adapts the old-style tool interface to the new BaseTool system.
    Existing tools that define name, description, and parameters properties
    can continue to work without modification.
    """

    @property
    def definition(self) -> ToolDefinition:
        """Build ToolDefinition from legacy properties."""
        params = []
        raw_params = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))

        for name, schema in raw_params.items():
            param = ToolParameter(
                name=name,
                description=schema.get("description", ""),
                type=schema.get("type", "string"),
                required=name in required,
                default=schema.get("default"),
                enum=schema.get("enum"),
            )
            params.append(param)

        _ro = getattr(self.__class__, "_is_read_only", False)
        _de = getattr(self.__class__, "_is_destructive", False)

        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=params,
            is_read_only=bool(_ro),
            is_destructive=bool(_de),
        )

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Tool description."""
        pass

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """Tool parameters in JSON Schema format."""
        pass

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        """Execute with legacy signature adaptation."""
        # Cast and validate
        casted = self.cast_params(params)
        errors = self.validate_params(casted)
        if errors:
            return f"Error: {'; '.join(errors)}"

        # Call legacy execute method with context available
        return await self._legacy_execute(**casted, _tool_context=context)

    @abstractmethod
    async def _legacy_execute(self, **kwargs: Any) -> Any:
        """Legacy execute method - override in subclasses."""
        pass
