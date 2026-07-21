"""Delegation control plane for subagent spawn limits.

Sits *above* :class:`CapabilityToken` (what a child may do) and enforces
process-level limits: spawn depth, concurrency, hard-blocked tools, and
default child approval mode. Mirrors Hermes-style leaf/orchestrator
boundaries without hard-coding them into CapabilityToken itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from markbot.agent.subagent.capability import CapabilityToken

# Tools that must never be available to subagents by default (prevent
# recursive spawn storms, outbound spam, and schedule side-effects).
DEFAULT_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "spawn",
    "message",
    "cron",
    "ask_user_question",
    "skill_manage",
    "computer_use",
})


@dataclass(frozen=True)
class DelegationPolicy:
    """Hard limits for parent→child delegation."""

    max_spawn_depth: int = 1
    max_concurrent_children: int = 3
    max_children_per_session: int = 8
    blocked_tools: frozenset[str] = DEFAULT_BLOCKED_TOOLS
    # When True, children default to AUTO permission (no interactive ask).
    # Interactive approval inside a headless child deadlocks stdin.
    force_auto_permission: bool = True
    # Default capability applied when spawn omits capability.
    default_capability_factory: str = "read_only"
    # Allow children to spawn further children (orchestrator role).
    allow_nested_spawn: bool = False

    def with_overrides(self, **kwargs: Any) -> "DelegationPolicy":
        data = {
            "max_spawn_depth": self.max_spawn_depth,
            "max_concurrent_children": self.max_concurrent_children,
            "max_children_per_session": self.max_children_per_session,
            "blocked_tools": self.blocked_tools,
            "force_auto_permission": self.force_auto_permission,
            "default_capability_factory": self.default_capability_factory,
            "allow_nested_spawn": self.allow_nested_spawn,
        }
        data.update(kwargs)
        if "blocked_tools" in kwargs and not isinstance(kwargs["blocked_tools"], frozenset):
            data["blocked_tools"] = frozenset(kwargs["blocked_tools"] or ())
        return DelegationPolicy(**data)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "DelegationPolicy":
        if not data:
            return cls()
        blocked = data.get("blocked_tools", DEFAULT_BLOCKED_TOOLS)
        if isinstance(blocked, str):
            blocked = [blocked]
        return cls(
            max_spawn_depth=int(data.get("max_spawn_depth", 1)),
            max_concurrent_children=int(data.get("max_concurrent_children", 3)),
            max_children_per_session=int(data.get("max_children_per_session", 8)),
            blocked_tools=frozenset(blocked),
            force_auto_permission=bool(data.get("force_auto_permission", True)),
            default_capability_factory=str(
                data.get("default_capability_factory", "read_only")
            ),
            allow_nested_spawn=bool(data.get("allow_nested_spawn", False)),
        )

    def default_capability(self) -> CapabilityToken:
        if self.default_capability_factory == "read_only":
            return CapabilityToken.read_only()
        return CapabilityToken.read_only()

    def harden_capability(self, capability: CapabilityToken | None) -> CapabilityToken:
        """Merge policy blocked tools into a capability token."""
        if capability is None:
            capability = self.default_capability()
        merged_forbidden = tuple(
            dict.fromkeys([*capability.forbidden_tools, *sorted(self.blocked_tools)])
        )
        if not self.allow_nested_spawn and "spawn" not in merged_forbidden:
            merged_forbidden = (*merged_forbidden, "spawn")
        if capability.forbidden_tools == merged_forbidden:
            return capability
        return CapabilityToken(
            allowed_tools=capability.allowed_tools,
            forbidden_tools=merged_forbidden,
            max_iterations=capability.max_iterations,
            max_budget_usd=capability.max_budget_usd,
            timeout_seconds=capability.timeout_seconds,
            description=capability.description,
            metadata=dict(capability.metadata),
        )

    def check_can_spawn(
        self,
        *,
        current_depth: int = 0,
        running_children: int = 0,
        session_child_count: int = 0,
    ) -> tuple[bool, str]:
        """Return (ok, reason). reason is empty when ok."""
        if current_depth >= self.max_spawn_depth:
            return (
                False,
                f"Spawn depth limit reached ({current_depth}/{self.max_spawn_depth}). "
                "Subagents cannot nest further.",
            )
        if running_children >= self.max_concurrent_children:
            return (
                False,
                f"Too many concurrent subagents "
                f"({running_children}/{self.max_concurrent_children}). "
                "Wait for one to finish or cancel with /stop.",
            )
        if session_child_count >= self.max_children_per_session:
            return (
                False,
                f"Session subagent quota exhausted "
                f"({session_child_count}/{self.max_children_per_session}).",
            )
        return True, ""


@dataclass
class DelegationTracker:
    """Runtime counters for a SubagentManager instance."""

    policy: DelegationPolicy = field(default_factory=DelegationPolicy)
    # parent_task_id -> depth (root parent uses depth 0)
    depths: dict[str, int] = field(default_factory=dict)
    session_spawn_counts: dict[str, int] = field(default_factory=dict)

    def depth_of(self, parent_id: str | None) -> int:
        if not parent_id:
            return 0
        return self.depths.get(parent_id, 0)

    def register_child(self, task_id: str, parent_id: str | None, session_key: str | None) -> int:
        parent_depth = self.depth_of(parent_id)
        child_depth = parent_depth + 1
        self.depths[task_id] = child_depth
        if session_key:
            self.session_spawn_counts[session_key] = (
                self.session_spawn_counts.get(session_key, 0) + 1
            )
        return child_depth

    def unregister(self, task_id: str) -> None:
        self.depths.pop(task_id, None)

    def session_count(self, session_key: str | None) -> int:
        if not session_key:
            return 0
        return self.session_spawn_counts.get(session_key, 0)
