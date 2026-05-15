from __future__ import annotations

from typing import Any

from markbot.utils.helpers import strip_ansi

_MAX_CONSOLE_MSG_LEN = 4000
_MAX_FILE_MSG_LEN = 10000


def _escape_markup(text: str) -> str:
    """Escape special characters for loguru's format pipeline.

    Three categories of escaping are needed:

    1. ``\\`` → ``\\\\`` — backslash must be escaped first so that
       subsequent replacements (``\\<``, ``{{``) are not double-escaped.
    2. ``<`` → ``\\<`` — prevents loguru's *Colorizer* from treating
       ``<`` as the start of a markup tag (e.g. ``<red>...</red>``).
    3. ``{`` → ``{{`` / ``}`` → ``}}`` — prevents Python's
       :func:`string.Formatter.parse` and :func:`str.format_map` from
       interpreting literal braces as format-field delimiters.  This is
       the root cause of ``ValueError: unmatched '{' in format spec`` and
       ``KeyError`` when log messages contain dict reprs such as
       ``Headers({'cache-control': ...})`` or tool-call payloads.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("<", "\\<")
        .replace("{", "{{")
        .replace("}", "}}")
    )


def console_format(record: dict[str, Any]) -> str:
    """Rich-coloured format for the stderr console sink."""
    msg = record["message"]
    if len(msg) > _MAX_CONSOLE_MSG_LEN:
        record["message"] = msg[:_MAX_CONSOLE_MSG_LEN] + "... [truncated]"

    name = _escape_markup(record["name"])
    function = _escape_markup(record["function"])
    line = record["line"]
    message = _escape_markup(record["message"])

    component = record["extra"].get("component", "")
    comp_tag = f"<blue>[{_escape_markup(component)}]</blue> " if component else ""

    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        f"<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        + comp_tag
        + f"<level>{message}</level>\n"
    )


def file_format(record: dict[str, Any]) -> str:
    """Plain-text format for the rotating file sink."""
    time_str = record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    level_str = f"{record['level'].name: <8}"
    msg = strip_ansi(record["message"])
    if len(msg) > _MAX_FILE_MSG_LEN:
        msg = msg[:_MAX_FILE_MSG_LEN] + "... [truncated]"

    component = record["extra"].get("component", "")
    comp_tag = f"[{component}] " if component else ""

    name = _escape_markup(record["name"])
    function = _escape_markup(record["function"])
    msg = _escape_markup(msg)

    return f"{time_str} | {level_str} | {name}:{function}:{record['line']} - {comp_tag}{msg}\n"
