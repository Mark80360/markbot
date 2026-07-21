"""Slash command routing and built-in handlers."""

from markbot.cli.slash_commands.builtin import (
    build_builtin_catalog,
    register_builtin_commands,
)
from markbot.cli.slash_commands.command_def import (
    CommandCatalog,
    CommandDef,
    CommandSurface,
    CommandTier,
)
from markbot.cli.slash_commands.router import CommandContext, CommandRouter

__all__ = [
    "CommandCatalog",
    "CommandContext",
    "CommandDef",
    "CommandRouter",
    "CommandSurface",
    "CommandTier",
    "build_builtin_catalog",
    "register_builtin_commands",
]
