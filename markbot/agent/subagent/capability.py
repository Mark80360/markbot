"""Capability-based delegation token for subagent control.

AI explicitly declares what a subagent is allowed to do, rather than
the framework imposing hard sandboxing rules.  The delegating AI is
responsible for setting appropriate boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
            description=description,
        )

    def allows(self, tool_name: str) -> bool:
        """Check if a tool is allowed by this token."""
        if tool_name in self.forbidden_tools:
            return False
        if not self.allowed_tools:
            return True  # empty = inherit/no restriction
        return tool_name in self.allowed_tools
