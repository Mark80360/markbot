"""Deferred cache-mutation policy for prefix-stable agent turns.

Per-conversation prompt caching is sacred: mid-turn tool-surface or system-
prompt changes bust the cached prefix and raise cost. This module records
mutations requested during an active turn and applies them only when a new
session/turn boundary is reached (or when the operator forces ``now=True``).

``AgentLoop`` owns one instance per loop, built from
``ToolsConfig.cache_policy``. There is no process-wide shared policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping


class MutationKind(str, Enum):
    TOOLS = "tools"
    SYSTEM_PROMPT = "system_prompt"
    SKILLS = "skills"
    PROFILE = "profile"
    OTHER = "other"


@dataclass(frozen=True)
class DeferredMutation:
    kind: MutationKind
    description: str
    apply: Callable[[], None]
    force_now: bool = False


@dataclass
class CacheMutationPolicy:
    """Queue mutations that would invalidate the prompt-cache prefix.

    Usage::

        policy = CacheMutationPolicy.from_settings(cfg.tools.cache_policy)
        policy.begin_turn()
        # During the turn:
        policy.request(MutationKind.TOOLS, "disable web_search", fn)
        # End of turn / new session:
        applied = policy.end_turn()
    """

    defer_mutations: bool = True
    active_turn: bool = False
    pending: list[DeferredMutation] = field(default_factory=list)
    applied_log: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "CacheMutationPolicy":
        """Build from ``ToolsConfig.cache_policy`` or a plain mapping."""
        if settings is None:
            return cls()
        if isinstance(settings, Mapping):
            defer = settings.get("defer_mutations", True)
        else:
            defer = getattr(settings, "defer_mutations", True)
        return cls(defer_mutations=bool(defer))

    def begin_turn(self) -> None:
        self.active_turn = True

    def end_turn(self) -> list[str]:
        """Mark turn finished and apply any deferred mutations."""
        self.active_turn = False
        return self.apply_pending()

    def request(
        self,
        kind: MutationKind | str,
        description: str,
        apply: Callable[[], None],
        *,
        now: bool = False,
    ) -> str:
        """Request a cache-sensitive mutation.

        Returns a short status string for the caller / slash command.

        Apply immediately when:
          - ``now=True`` (operator force)
          - ``defer_mutations`` is False (config)
          - no active turn (idle / between turns)
        Otherwise queue until :meth:`end_turn` / :meth:`apply_pending`.
        """
        if isinstance(kind, str):
            try:
                kind = MutationKind(kind)
            except ValueError:
                kind = MutationKind.OTHER

        mut = DeferredMutation(
            kind=kind,
            description=description,
            apply=apply,
            force_now=now,
        )

        if now or not self.defer_mutations or not self.active_turn:
            self._apply_one(mut)
            return f"Applied now: {description}"

        self.pending.append(mut)
        return (
            f"Deferred until next turn (cache-safe): {description}. "
            "Pass now=True to force immediate apply (busts prefix cache)."
        )

    def apply_pending(self) -> list[str]:
        """Apply all queued mutations. Safe to call when empty."""
        if not self.pending:
            return []
        applied: list[str] = []
        queue, self.pending = self.pending, []
        for mut in queue:
            self._apply_one(mut)
            applied.append(mut.description)
        return applied

    def peek_pending(self) -> list[str]:
        return [m.description for m in self.pending]

    def clear(self) -> None:
        self.pending.clear()

    def _apply_one(self, mut: DeferredMutation) -> None:
        mut.apply()
        self.applied_log.append(mut.description)
        if len(self.applied_log) > 50:
            self.applied_log = self.applied_log[-50:]
