"""Tool-surface footprint ladder — allocate attention by what the model sees.

Goals:
  1. Keep the model-facing tool schema small (prefix-cache friendly).
  2. Soft-disable tools without unregistering them (can re-enable later).
  3. Apply profile denylists via soft-disable so mid-session rebind can
     go through CacheMutationPolicy without re-importing tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from markbot.config.profile import TOOL_GROUPS


PROFILE_GROUP_FLAGS: dict[str, str] = {
    "desktop": "enable_desktop",
    "browser": "enable_browser",
    "autopilot": "enable_autopilot",
}


@dataclass
class FootprintSnapshot:
    """Current model-facing tool surface summary."""

    registered: int = 0
    available: int = 0
    soft_disabled: tuple[str, ...] = ()
    available_names: tuple[str, ...] = ()
    profile: str = ""
    pending_mutations: tuple[str, ...] = ()

    def as_lines(self) -> list[str]:
        lines = [
            f"profile={self.profile or '?'}",
            f"tools available={self.available}/{self.registered}",
        ]
        if self.soft_disabled:
            shown = ", ".join(self.soft_disabled[:12])
            extra = (
                f" (+{len(self.soft_disabled) - 12})"
                if len(self.soft_disabled) > 12
                else ""
            )
            lines.append(f"soft-disabled: {shown}{extra}")
        if self.pending_mutations:
            lines.append(
                "pending cache mutations: " + "; ".join(self.pending_mutations[:5])
            )
        return lines


@dataclass
class ToolFootprint:
    """Soft-disable layer that pairs with ToolRegistry."""

    soft_disabled: set[str] = field(default_factory=set)
    profile_name: str = ""

    def apply_profile(
        self,
        profile: Any | None,
        *,
        extra_disabled: Iterable[str] | None = None,
    ) -> set[str]:
        """Compute and store the soft-disabled set for *profile*."""
        disabled: set[str] = set()
        if profile is not None:
            self.profile_name = str(getattr(profile, "name", "") or "")
            for name in getattr(profile, "disabled_tools", ()) or ():
                disabled.add(str(name))
            for group, flag in PROFILE_GROUP_FLAGS.items():
                if not getattr(profile, flag, True):
                    disabled |= set(TOOL_GROUPS.get(group, ()))
            if not getattr(profile, "enable_explore", True):
                disabled |= {
                    "explore",
                    "explore_context_catalog",
                    "search_context",
                    "load_context",
                }
            if not getattr(profile, "enable_subagents", True):
                disabled |= {"spawn", "check_subagent", "list_subagents"}
        if extra_disabled:
            disabled |= {str(n) for n in extra_disabled}
        self.soft_disabled = disabled
        return set(disabled)

    def disable(self, *names: str) -> None:
        self.soft_disabled.update(names)

    def enable(self, *names: str) -> None:
        for n in names:
            self.soft_disabled.discard(n)

    def is_disabled(self, name: str) -> bool:
        return name in self.soft_disabled

    def snapshot(
        self,
        registry: Any,
        *,
        pending_mutations: Iterable[str] | None = None,
    ) -> FootprintSnapshot:
        registered = list(getattr(registry, "tool_names", []) or [])
        soft = tuple(sorted(self.soft_disabled))
        try:
            available = [d.name for d in registry.definitions]
        except Exception:
            available = [n for n in registered if n not in self.soft_disabled]
        return FootprintSnapshot(
            registered=len(registered),
            available=len(available),
            soft_disabled=soft,
            available_names=tuple(available),
            profile=self.profile_name,
            pending_mutations=tuple(pending_mutations or ()),
        )


def apply_footprint_to_registry(registry: Any, footprint: ToolFootprint) -> None:
    """Push soft-disabled names into the registry and bust schema cache."""
    if hasattr(registry, "set_soft_disabled"):
        registry.set_soft_disabled(footprint.soft_disabled)
    elif hasattr(registry, "invalidate_definitions_cache"):
        registry.invalidate_definitions_cache()
