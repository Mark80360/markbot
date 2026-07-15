"""Public UI helpers for the MarkBot CLI.

Extracted from ``markbot.cli.commands``. These functions have no
dependency on typer or any specific command group — they are pure
rendering / TTY helpers safe to import from anywhere under
``markbot.cli.*``.

The module also owns the CLI's global :data:`console` (Rich) and
the prompt_toolkit session singleton (:data:`PROMPT_SESSION`) so
that helpers which need a console or session can use a shared
instance without each callsite re-creating one.
"""

from __future__ import annotations

import os
import select
import sys
from contextlib import nullcontext
from typing import Any, TYPE_CHECKING

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from markbot import __logo__
from markbot.cli.stream import ThinkingSpinner

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession

__all__ = [
    "markbot_banner",
    "flush_pending_tty_input",
    "get_prompt_session",
    "restore_terminal",
    "init_prompt_session",
    "make_console",
    "render_interactive_ansi",
    "print_agent_response",
    "response_renderable",
    "print_cli_progress_line",
    "is_exit_command",
    "make_section_helpers",
]

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Shared Rich console for the whole CLI. Helpers that need a console use
# this directly; the few entry points that need a fresh console (e.g. to
# capture output) call :func:`make_console`.
console: Console = Console()

# Subset of strings that should end an interactive chat session.
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# prompt_toolkit session singleton, lazily initialised by
# :func:`init_prompt_session` and read by the interactive read helper
# in :mod:`markbot.cli.commands`.
PROMPT_SESSION: "PromptSession | None" = None

# Original termios settings, restored on exit. Saved by
# :func:`init_prompt_session`.
SAVED_TERM_ATTRS: Any = None


# ---------------------------------------------------------------------------
# Banner / TTY
# ---------------------------------------------------------------------------


def markbot_banner() -> None:
    console.print()
    console.print("  ███╗░░░███╗░█████╗░██████╗░██╗░░██╗██████╗░░█████╗░░███████╗")
    console.print("  ████╗░████║██╔══██╗██╔══██╗██║░██╔╝██╔══██╗██╔══██╗╚══██╔══╝")
    console.print("  ██╔████╔██║███████║██████╔╝█████═╝░██████╦╝██║░░██║░░░██║░░░")
    console.print("  ██║╚██╔╝██║██╔══██║██╔══██╗██╔═██╗░██╔══██╗██║░░██║░░░██║░░░")
    console.print("  ██║░╚═╝░██║██║░░██║██║░░██║██║░╚██╗██████╦╝╚█████╔╝░░░██║░░░")
    console.print("  ╚═╝░░░░░╚═╝╚═╝░░╚═╝╚═╝░░╚═╝╚═╝░░╚═╝╚═════╝░░╚════╝░░░░╚═╝░░░")


def flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, SAVED_TERM_ATTRS)
    except Exception:
        pass


def init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global PROMPT_SESSION, SAVED_TERM_ATTRS

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Interactive agent mode requires prompt_toolkit. "
            "Install project dependencies with `pip install -e .`."
        ) from exc

    # Save terminal state so we can restore it on exit
    try:
        import termios
        SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from markbot.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def make_console() -> Console:
    return Console(file=sys.stdout)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely."""
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Render assistant response with consistent terminal styling."""
    out = make_console()
    content = response or ""
    body = response_renderable(content, render_markdown, metadata)
    out.print()
    out.print(f"[cyan]{__logo__} MarkBot[/cyan]")
    out.print(body)
    out.print()


def response_renderable(content: str, render_markdown: bool, metadata: dict | None = None):
    """Render plain-text command output without markdown collapsing newlines."""
    if not render_markdown:
        return Text(content)
    if (metadata or {}).get("render_as") == "text":
        return Text(content)
    return Markdown(content)


def print_cli_progress_line(text: str, thinking: ThinkingSpinner | None) -> None:
    """Print a CLI progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        console.print(f"  [dim]↳ {text}[/dim]")


def get_prompt_session() -> "PromptSession | None":
    """Return the current prompt_toolkit session singleton."""
    return PROMPT_SESSION


def is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


# ---------------------------------------------------------------------------
# Shared section/kv/divider helpers
# ---------------------------------------------------------------------------

# Default width used by the various CLI status sub-commands. Kept here so
# all callers render at the same width and can be tweaked in one place.
DEFAULT_STATUS_WIDTH = 72


def make_section_helpers(width: int = DEFAULT_STATUS_WIDTH):
    """Return a tuple of (section, kv, divider) closures bound to ``console``.

    Multiple CLI groups (channels/config/gateway/plugins/provider/status/...)
    re-defined the same three local helpers. Centralising them here keeps the
    rendering style consistent and lets us change the look in one place.
    """
    W = width

    def section(title: str, color: str = "cyan") -> None:
        title_text = f"  {title}  "
        pad = W - len(title_text) - 2
        line = Text.from_markup(f"[{color}]{title_text}[/][dim]{'─' * pad}[/]")
        console.print(line)

    def kv(key: str, value: str, key_w: int = 14) -> None:
        line = Text.from_markup(f"  [cyan]{key:<{key_w}}[/cyan] {value}")
        console.print(line)

    def divider() -> None:
        line = Text.from_markup(f"[dim]{'─' * (W - 2)}[/]")
        console.print(line)

    return section, kv, divider
