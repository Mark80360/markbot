"""Daily interaction log manager.

Appends raw user/assistant interaction logs to workspace/memory/daily/YYYY-MM-DD.md
files. This is a lightweight, no-LLM audit trail — just file I/O.

Inspired by CoPaw's daily memory logs and the Warm Memory concept from
the Tiered Memory Architecture guide.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


_DEFAULT_MAX_CONTENT_LENGTH = 2000


class DailyLogManager:
    """Append-only daily interaction log writer.

    Each day gets its own markdown file under ``workspace/memory/daily/``.
    Entries are appended atomically (open → seek end → write → close)
    so concurrent turns don't clobber each other.
    """

    def __init__(
        self,
        workspace: Path,
        max_content_length: int = _DEFAULT_MAX_CONTENT_LENGTH,
    ):
        self._daily_dir: Path = Path(workspace) / "memory" / "daily"
        self._daily_dir.mkdir(parents=True, exist_ok=True)
        self._max_content_length = max_content_length

    def append_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        channel: str = "",
        chat_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one user-assistant turn to today's daily log.

        Args:
            user_content: Raw user message text.
            assistant_content: Raw assistant response text.
            channel: Source channel (e.g. "cli", "dingtalk").
            chat_id: Chat/session identifier.
            metadata: Optional extra metadata dict.
        """
        now = datetime.now()
        filename = f"{now.strftime('%Y-%m-%d')}.md"
        filepath = self._daily_dir / filename

        if not filepath.exists():
            header = f"# Daily Log: {now.strftime('%Y-%m-%d')}\n\n"
        else:
            header = ""

        timestamp = now.strftime("%H:%M:%S")
        session_tag = f" `{chat_id}`" if chat_id else ""
        channel_tag = f" via {channel}" if channel else ""

        user_text = self._truncate(user_content or "")
        assistant_text = self._truncate(assistant_content or "")

        parts: list[str] = []
        if header:
            parts.append(header)

        parts.append(f"## [{timestamp}]{session_tag}{channel_tag}\n")
        parts.append(f"**User**:\n\n{user_text}\n")
        parts.append("---\n")
        parts.append(f"**Assistant**:\n\n{assistant_text}\n")
        parts.append("---\n")

        entry = "\n".join(parts)

        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as e:
            logger.warning(f"[DailyLog] Failed to append to {filepath}: {e}")

    def _truncate(self, text: str) -> str:
        if len(text) <= self._max_content_length:
            return text
        return text[: self._max_content_length] + "\n\n... [truncated]"
