"""Single-source slash command definitions.

``CommandDef`` describes a command once; :class:`CommandRouter` still does
dispatch, but help text / autocomplete / channel filtering can all derive
from this registry instead of hard-coded strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable

if TYPE_CHECKING:
    from markbot.cli.slash_commands.router import CommandContext, Handler


class CommandTier(str, Enum):
    """Dispatch tier for the command router."""

    PRIORITY = "priority"  # bypass session lock (/stop, /steer, …)
    EXACT = "exact"
    PREFIX = "prefix"


class CommandSurface(str, Enum):
    CLI = "cli"
    GATEWAY = "gateway"
    ALL = "all"


@dataclass(frozen=True)
class CommandDef:
    name: str
    handler: "Handler"
    description: str
    tier: CommandTier = CommandTier.EXACT
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    category: str = "general"
    surface: CommandSurface = CommandSurface.ALL
    # Control-plane commands must not queue behind work-plane messages.
    control_plane: bool = False

    @property
    def canonical(self) -> str:
        return self.name if self.name.startswith("/") else f"/{self.name}"

    def all_names(self) -> tuple[str, ...]:
        names = [self.canonical]
        for a in self.aliases:
            names.append(a if a.startswith("/") else f"/{a}")
        return tuple(names)

    def help_line(self) -> str:
        hint = f" {self.args_hint}" if self.args_hint else ""
        return f"{self.canonical}{hint} — {self.description}"


@dataclass
class CommandCatalog:
    """Ordered registry of CommandDef entries."""

    _items: list[CommandDef] = field(default_factory=list)

    def register(self, *defs: CommandDef) -> None:
        self._items.extend(defs)

    def __iter__(self):
        return iter(self._items)

    def list_for_surface(self, surface: str | CommandSurface = CommandSurface.ALL) -> list[CommandDef]:
        if isinstance(surface, str):
            try:
                surface = CommandSurface(surface)
            except ValueError:
                surface = CommandSurface.ALL
        out: list[CommandDef] = []
        for d in self._items:
            if d.surface is CommandSurface.ALL or d.surface is surface or surface is CommandSurface.ALL:
                out.append(d)
        return out

    def help_text(self, surface: str | CommandSurface = CommandSurface.ALL) -> str:
        lines = ["MarkBot commands:"]
        for d in self.list_for_surface(surface):
            lines.append(d.help_line())
        return "\n".join(lines)

    def names(self) -> list[str]:
        names: list[str] = []
        for d in self._items:
            names.extend(d.all_names())
        return names

    def apply_to_router(self, router: "CommandRouterLike") -> None:
        """Register every def onto a CommandRouter-compatible object."""
        for d in self._items:
            names = d.all_names()
            primary = names[0]
            if d.tier is CommandTier.PRIORITY or d.control_plane:
                router.priority(primary, d.handler)
                for alias in names[1:]:
                    router.priority(alias, d.handler)
            elif d.tier is CommandTier.PREFIX:
                # Prefix entries keep trailing space semantics.
                pfx = primary if primary.endswith(" ") else primary + " "
                router.prefix(pfx, d.handler)
            else:
                router.exact(primary, d.handler)
                for alias in names[1:]:
                    router.exact(alias, d.handler)


class CommandRouterLike:
    """Structural protocol duck-typed against CommandRouter."""

    def priority(self, cmd: str, handler: Callable) -> None: ...
    def exact(self, cmd: str, handler: Callable) -> None: ...
    def prefix(self, pfx: str, handler: Callable) -> None: ...
