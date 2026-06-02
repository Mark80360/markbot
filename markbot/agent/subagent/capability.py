"""Capability-based delegation token for subagent control.

AI explicitly declares what a subagent is allowed to do, rather than
the framework imposing hard sandboxing rules.  The delegating AI is
responsible for setting appropriate boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class CapabilityToken:
    """An AI-declared capability boundary for subagent execution.

    The delegating AI explicitly states what tools/actions a subagent
    may perform, and what it must NOT do.  The framework trusts this
    declaration — it does not enforce it via OS-level sandboxing.

    Example::

        CapabilityToken(
            allowed_tools=("read_file", "glob", "grep"),
            forbidden_tools=("exec", "write_file"),
            max_iterations=10,
            description="Read-only code review",
        )
    """

    allowed_tools: tuple[str, ...] = ()
    """Tools the subagent is permitted to use.  Empty = inherit from parent."""

    forbidden_tools: tuple[str, ...] = ()
    """Tools the subagent must NOT use, even if otherwise allowed."""

    max_iterations: int = 15
    """Maximum tool-call iterations before the subagent must respond."""

    max_budget_usd: float | None = None
    """Maximum cost in USD this subagent may incur. None = no subagent-level limit."""

    timeout_seconds: float | None = None
    """Maximum wall-clock time for the subagent. None = no timeout."""

    description: str = ""
    """Human-readable summary of what this delegation covers."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extensibility slot for future constraints (cost limit, timeout, etc.)."""

    @staticmethod
    def read_only(description: str = "Read-only research") -> CapabilityToken:
        """Factory for a common read-only capability profile."""
        return CapabilityToken(
            allowed_tools=(
                "read_file", "glob", "grep",
                "web_search", "web_fetch", "web_extract",
            ),
            forbidden_tools=(
                "exec", "write_file", "edit_file", "delete_file",
                "message", "spawn", "ask_user_question",
            ),
            max_budget_usd=0.5,
            timeout_seconds=300,
            description=description,
        )

    def allows(self, tool_name: str) -> bool:
        """Check if a tool is allowed by this token."""
        if tool_name in self.forbidden_tools:
            return False
        if not self.allowed_tools:
            return True  # empty = inherit/no restriction
        return tool_name in self.allowed_tools

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation.

        Used to pass a capability through the LLM tool call boundary:
        the LLM declares the capability as a JSON object, which the
        receiving side parses back via :meth:`from_dict`.
        """
        return {
            "allowed_tools": list(self.allowed_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "max_iterations": self.max_iterations,
            "max_budget_usd": self.max_budget_usd,
            "timeout_seconds": self.timeout_seconds,
            "description": self.description,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "CapabilityToken":
        """Build a :class:`CapabilityToken` from a JSON-like mapping.

        Accepts both snake_case (canonical) and camelCase (LLM-friendly)
        keys. Unknown keys are preserved in ``metadata`` so future
        extensions round-trip cleanly. ``None`` returns
        :meth:`read_only` so callers can blindly forward a possibly
        absent ``capability`` argument from a tool call.
        """
        if data is None:
            return cls.read_only()
        if not isinstance(data, Mapping):
            raise TypeError(
                f"capability must be a mapping, got {type(data).__name__}"
            )

        def _str_tuple(value: Any, field_name: str) -> tuple[str, ...]:
            if value is None:
                return ()
            if isinstance(value, str):
                return (value,)
            if isinstance(value, (list, tuple)):
                bad = [v for v in value if not isinstance(v, str)]
                if bad:
                    raise ValueError(
                        f"{field_name} entries must be strings; "
                        f"got {[type(v).__name__ for v in bad]}"
                    )
                return tuple(value)
            raise ValueError(
                f"{field_name} must be a list of strings, got {type(value).__name__}"
            )

        def _opt_float(value: Any, field_name: str) -> float | None:
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"{field_name} must be a number, got {value!r}"
                ) from e

        def _opt_int(value: Any, field_name: str, default: int) -> int:
            if value is None or value == "":
                return default
            try:
                return int(value)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"{field_name} must be an integer, got {value!r}"
                ) from e

        # Accept both snake_case and camelCase aliases.
        allowed = _str_tuple(
            data.get("allowed_tools", data.get("allowedTools")),
            "allowed_tools",
        )
        forbidden = _str_tuple(
            data.get("forbidden_tools", data.get("forbiddenTools")),
            "forbidden_tools",
        )
        max_iterations = _opt_int(
            data.get("max_iterations", data.get("maxIterations")),
            "max_iterations",
            default=15,
        )
        max_budget = _opt_float(
            data.get("max_budget_usd", data.get("maxBudgetUsd")),
            "max_budget_usd",
        )
        timeout = _opt_float(
            data.get("timeout_seconds", data.get("timeoutSeconds")),
            "timeout_seconds",
        )
        description = data.get("description", "")
        if not isinstance(description, str):
            description = str(description)
        metadata_raw = data.get("metadata") or {}
        if not isinstance(metadata_raw, Mapping):
            metadata_raw = {}
        metadata = dict(metadata_raw)

        # Stash unknown keys in metadata so round-trips are lossless.
        known = {
            "allowed_tools", "allowedTools",
            "forbidden_tools", "forbiddenTools",
            "max_iterations", "maxIterations",
            "max_budget_usd", "maxBudgetUsd",
            "timeout_seconds", "timeoutSeconds",
            "description", "metadata",
        }
        for key, value in data.items():
            if key not in known:
                metadata[key] = value

        return cls(
            allowed_tools=allowed,
            forbidden_tools=forbidden,
            max_iterations=max_iterations,
            max_budget_usd=max_budget,
            timeout_seconds=timeout,
            description=description,
            metadata=metadata,
        )
