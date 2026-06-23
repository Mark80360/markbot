"""Utility functions for markbot."""

import base64
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def shorten(text: str, *, limit: int = 120) -> str:
    """Normalize whitespace and truncate text to *limit* characters."""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def strip_think(text: str | None) -> str | None:
    """Remove  blocks and any unclosed trailing  tag."""
    if not text:
        return text
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    text = re.sub(r"<think>[\s\S]*$", "", text)
    return text.strip()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b_P[^\x1b]*\x1b\\|\x1b\][^\x00]*\x00")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text (color, cursor, etc.)."""
    return _ANSI_RE.sub("", text)


def detect_image_mime(data: bytes) -> str | None:
    """Detect image MIME type from magic bytes, ignoring file extension."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def build_image_content_blocks(raw: bytes, mime: str, path: str, label: str) -> list[dict[str, Any]]:
    """Build native image blocks plus a short text label."""
    b64 = base64.b64encode(raw).decode()
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
            "_meta": {"path": path},
        },
        {"type": "text", "text": label},
    ]


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


def normalize_timezone(tz: str | None) -> str:
    """Convert offset-style timezone strings (e.g. ``"UTC+8"``) to IANA names.

    Returns *tz* unchanged when it is already a valid IANA name or ``None``.
    Falls back to ``"UTC"`` for unrecognised offsets.
    """
    if not tz:
        return "UTC"

    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(tz)
        return tz
    except (KeyError, Exception):
        pass

    _OFFSET_MAP: dict[str, str] = {
        "UTC-12": "Etc/GMT+12", "UTC-11": "Pacific/Pago_Pago",
        "UTC-10": "Pacific/Honolulu", "UTC-9:30": "Pacific/Marquesas",
        "UTC-9": "America/Anchorage", "UTC-8": "America/Los_Angeles",
        "UTC-7": "America/Denver", "UTC-6": "America/Chicago",
        "UTC-5": "America/New_York", "UTC-4:30": "America/Caracas",
        "UTC-4": "America/Santiago", "UTC-3:30": "America/St_Johns",
        "UTC-3": "America/Sao_Paulo", "UTC-2": "America/Noronha",
        "UTC-1": "Atlantic/Azores", "UTC+0": "UTC", "UTC+1": "Europe/Berlin",
        "UTC+2": "Africa/Cairo", "UTC+3": "Europe/Moscow",
        "UTC+3:30": "Asia/Tehran", "UTC+4": "Asia/Dubai",
        "UTC+4:30": "Asia/Kabul", "UTC+5": "Asia/Karachi",
        "UTC+5:30": "Asia/Kolkata", "UTC+5:45": "Asia/Kathmandu",
        "UTC+6": "Asia/Dhaka", "UTC+6:30": "Asia/Yangon",
        "UTC+7": "Asia/Bangkok", "UTC+8": "Asia/Shanghai",
        "UTC+8:45": "Australia/Eucla", "UTC+9": "Asia/Tokyo",
        "UTC+9:30": "Australia/Darwin", "UTC+10": "Australia/Sydney",
        "UTC+10:30": "Australia/Lord_Howe", "UTC+11": "Pacific/Noumea",
        "UTC+12": "Pacific/Auckland", "UTC+12:45": "Pacific/Chatham",
        "UTC+13": "Pacific/Tongatapu", "UTC+14": "Pacific/Kiritimati",
    }

    normalized = _OFFSET_MAP.get(tz)
    if normalized:
        try:
            ZoneInfo(normalized)
            return normalized
        except (KeyError, Exception):
            pass

    from loguru import logger
    logger.warning("Unrecognised timezone '{}', falling back to UTC", tz)
    return "UTC"


def current_time_str(timezone: str | None = None) -> str:
    """Human-readable current time with weekday and UTC offset.

    When *timezone* is a valid IANA name (e.g. ``"Asia/Shanghai"``), the time
    is converted to that zone.  Otherwise falls back to the host local time.
    """
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone) if timezone else None
    except (KeyError, Exception):
        tz = None

    now = datetime.now(tz=tz) if tz else datetime.now().astimezone()
    offset = now.strftime("%z")
    offset_fmt = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    tz_name = timezone or (time.strftime("%Z") or "UTC")
    return f"{now.strftime('%Y-%m-%d %H:%M (%A)')} ({tz_name}, UTC{offset_fmt})"


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')

def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def format_time(timestamp: float) -> str:
    """Format a Unix timestamp as a human-readable datetime string."""
    from datetime import datetime
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def split_message(content: str, max_len: int = 2000) -> list[str]:
    """
    Split content into chunks within max_len, preferring line breaks.

    Args:
        content: The text content to split.
        max_len: Maximum length per chunk (default 2000).

    Returns:
        List of message chunks, each within max_len.
    """
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        # Try to break at newline first, then space, then hard break
        pos = cut.rfind('\n')
        if pos <= 0:
            pos = cut.rfind(' ')
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


def build_assistant_message(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    thinking_blocks: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a provider-safe assistant message with optional reasoning fields.

    Ensures ``content`` is never ``None`` — normalises to empty string.
    This prevents downstream crashes in serialisation / compaction / context-checking
    paths that assume content is always iterable or string-castable.

    ``reasoning_content`` is **always** written to the message, even when the
    model didn't return any reasoning on this turn (we substitute an empty
    string). Thinking-mode providers such as DeepSeek V4+ and Kimi K2 reject
    subsequent turns with HTTP 400 ``reasoning_content must be passed back``
    when the field is missing entirely once thinking mode is on, so the
    explicit empty string is the safest acknowledgement. See debug session
    ``markbot-multimodal-chain-fail``.
    """
    if content is None:
        content = ""
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    # Always emit ``reasoning_content``; substitute "" when absent so the
    # provider sees a stable shape across turns.
    msg["reasoning_content"] = reasoning_content if reasoning_content is not None else ""
    if thinking_blocks:
        msg["thinking_blocks"] = thinking_blocks
    return msg


def build_status_content(
    *,
    version: str,
    model: str,
    start_time: float,
    context_window_tokens: int,
    context_tokens: int,
    session_msg_count: int,
    session_history_count: int,
    tool_count: int,
    last_usage: dict[str, int],
    cumulative_input: int = 0,
    cumulative_output: int = 0,
    cumulative_cache_creation: int = 0,
    cumulative_cache_read: int = 0,
    api_calls: int = 0,
) -> str:
    """Build a human-readable runtime status snapshot."""
    uptime_s = int(time.time() - start_time)
    uptime = (
        f"{uptime_s // 3600}h {(uptime_s % 3600) // 60}m"
        if uptime_s >= 3600
        else f"{uptime_s // 60}m {uptime_s % 60}s"
    )

    ctx_total = max(context_window_tokens, 0)
    ctx_pct = int((context_tokens / ctx_total) * 100) if ctx_total > 0 else 0
    ctx_used = f"{context_tokens // 1000}k" if context_tokens >= 1000 else str(context_tokens)
    ctx_total_str = f"{ctx_total // 1024}k" if ctx_total > 0 else "n/a"

    last_in = last_usage.get("prompt_tokens", 0)
    last_out = last_usage.get("completion_tokens", 0)
    last_cache_create = last_usage.get("cache_creation_input_tokens", 0)
    last_cache_read = last_usage.get("cache_read_input_tokens", 0)

    def _fmt(n: int) -> str:
        return f"{n // 1000}k" if n >= 1000 else str(n)

    tokens_parts = [f"last: {_fmt(last_in)} in / {_fmt(last_out)} out"]
    if cumulative_input or cumulative_output:
        tokens_parts.append(f"total: {_fmt(cumulative_input)} in / {_fmt(cumulative_output)} out")
    cache_parts = []
    if last_cache_create or last_cache_read:
        cache_parts.append(f"last: +{_fmt(last_cache_create)} cr / {_fmt(last_cache_read)} rd")
    if cumulative_cache_creation or cumulative_cache_read:
        cache_parts.append(f"total: +{_fmt(cumulative_cache_creation)} cr / {_fmt(cumulative_cache_read)} rd")

    lines = [
        f"\U0001f99e MarkBot v{version}",
        f"\U0001f9e0 Model: {model}",
        f"\U0001f4ca Tokens: {' | '.join(tokens_parts)}",
    ]
    if cache_parts:
        lines.append(f"   \U0001f4be Cache: {' | '.join(cache_parts)}")
    lines.extend([
        f"\U0001f4da Context: {ctx_used}/{ctx_total_str} ({ctx_pct}%)",
        f"\U0001f4ac Session: {session_msg_count} stored ({session_history_count} active) | {tool_count} tools",
        f"\U0001f551 Uptime: {uptime} | API calls: {api_calls}",
    ])
    return "\n".join(lines)


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files.

    Recurses into subdirectories (e.g. ``agents/``), preserving structure.
    """
    from importlib.resources import files as pkg_files
    try:
        tpl = pkg_files("markbot") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    def _sync_dir(src_dir, dest_dir: Path):
        for item in src_dir.iterdir():
            if item.name.startswith("."):
                continue
            if item.is_dir():
                _sync_dir(item, dest_dir / item.name)
            elif item.is_file() and item.name.endswith(".md"):
                _write(item, dest_dir / item.name)

    _sync_dir(tpl, workspace)
    (workspace / "skills").mkdir(exist_ok=True)

    if added and not silent:
        from rich.console import Console
        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added
