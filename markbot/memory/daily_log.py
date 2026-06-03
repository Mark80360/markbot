"""Daily interaction log manager.

Appends raw user/assistant interaction logs to workspace/memory/daily/YYYY-MM-DD.md
files. This is a lightweight, no-LLM audit trail — just file I/O.

Inspired by daily memory logs and the Warm Memory concept from
the Tiered Memory Architecture guide.

Each entry is tagged with ``channel`` and ``chat_id`` so that
session-scoped searches can filter results to a single conversation.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.utils.constants import MAX_DAILY_LOG_RESULT_CHARS

_DEFAULT_MAX_CONTENT_LENGTH = 2000

# CJK Unified Ideographs — basic block (U+4E00..U+9FFF).
# Single-char matches miss most search queries like "用户喜欢Python" (the
# token "用户" only matches if the haystack contains the exact same two
# chars in the same order). Bigrams of consecutive CJK chars plus the
# single chars give both prefix and substring recall.
_CJK_BASIC_RANGE = r"\u4e00-\u9fff"
_TOKEN_RE = re.compile(rf"[a-zA-Z0-9]+|[{_CJK_BASIC_RANGE}]")


def tokenize_for_search(text: str) -> list[str]:
    """Split text into lowercase tokens with CJK bigram support.

    Used by daily log and memory entry search so both surfaces produce
    consistent recall. Returns a list containing:
      - every ASCII alnum run (lowercased)
      - every CJK character
      - every bigram where at least one side is CJK (covers "用户" /
        "用p" overlap so Latin neighbors don't form dead tokens)
    """
    if not text:
        return []
    tokens = _TOKEN_RE.findall(text.lower())
    bigrams: list[str] = []
    for i in range(len(tokens) - 1):
        if tokens[i][0] >= "\u4e00" or tokens[i + 1][0] >= "\u4e00":
            bigrams.append(tokens[i] + tokens[i + 1])
    return tokens + bigrams


_SECTION_HEADER_RE = re.compile(
    r"^## \[(?P<time>[^\]]+)\]"
    r"(?:\s+`(?P<chat_id>[^`]+)`)?"
    r"(?:\s+via\s+(?P<channel>\S+))?"
)


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

    @property
    def daily_dir(self) -> Path:
        return self._daily_dir

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
            fd = os.open(str(filepath), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, entry.encode("utf-8"))
            finally:
                os.close(fd)
        except Exception as e:
            logger.warning("Failed to append to {}: {}", filepath, e)

    def search(
        self,
        query: str,
        max_results: int = 5,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict]:
        """Keyword search over daily log files with optional session filtering.

        Uses token matching with CJK bigram support. When *channel* and/or
        *chat_id* are provided, only sections belonging to that session are
        considered.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return.
            channel: Optional channel filter (e.g. "feishu").
            chat_id: Optional chat ID filter.

        Returns:
            List of dicts with ``content``, ``source``, ``score`` keys.
        """
        if not self._daily_dir.is_dir():
            return []

        query_tokens = set(tokenize_for_search(query))
        if not query_tokens:
            return []

        candidates: list[tuple[float, str, str]] = []
        md_files = sorted(self._daily_dir.glob("*.md"), reverse=True)

        for md_file in md_files:
            try:
                text = md_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            sections = re.split(r"^## \[", text, flags=re.MULTILINE)
            for section in sections:
                if not section.strip():
                    continue

                header_line = section.split("\n", 1)[0]
                match = _SECTION_HEADER_RE.match("## [" + header_line)

                if match:
                    sec_channel = match.group("channel") or ""
                    sec_chat_id = match.group("chat_id") or ""

                    if channel and sec_channel != channel:
                        continue
                    if chat_id and sec_chat_id != chat_id:
                        continue

                section_text = section.lower()
                section_tokens = set(tokenize_for_search(section_text))
                hits = sum(1 for t in query_tokens if t in section_tokens)
                if hits == 0:
                    continue
                score = hits / len(query_tokens)
                section_len = max(len(section_text), 1)
                score = score * (1.0 / (1.0 + section_len / 10000.0))
                content = section.strip()
                if len(content) > MAX_DAILY_LOG_RESULT_CHARS:
                    content = content[:MAX_DAILY_LOG_RESULT_CHARS] + "\n... [truncated]"
                candidates.append((score, header_line[:80], content))

        candidates.sort(key=lambda x: x[0], reverse=True)

        results: list[dict] = []
        for score, header, content in candidates[:max_results]:
            results.append({
                "content": content,
                "source": f"daily/{header}",
                "score": round(score, 3),
            })

        return results

    def get_recent_user_messages(
        self,
        limit: int = 20,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
        days: int = 7,
    ) -> list[dict]:
        """Retrieve recent user messages chronologically from daily logs.

        Unlike ``search()`` which does keyword matching, this method reads
        daily log files and extracts user messages in reverse chronological
        order, optionally scoped to a specific session via channel/chat_id.

        Returns:
            List of dicts with ``timestamp``, ``content``, ``channel``,
            ``chat_id``, ``date`` keys, ordered newest-first.
        """
        from datetime import datetime, timedelta

        if not self._daily_dir.is_dir():
            return []

        cutoff = datetime.now() - timedelta(days=days)
        md_files = sorted(
            [f for f in self._daily_dir.glob("*.md") if f.stem >= cutoff.strftime("%Y-%m-%d")],
            reverse=True,
        )

        results: list[dict] = []
        for md_file in md_files:
            try:
                text = md_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            date_str = md_file.stem
            sections = re.split(r"^## \[", text, flags=re.MULTILINE)

            # Process sections in reverse (newest first within a file)
            for section in reversed(sections):
                if not section.strip():
                    continue

                header_line = section.split("\n", 1)[0]
                match = _SECTION_HEADER_RE.match("## [" + header_line)
                if not match:
                    continue

                sec_time = match.group("time") or ""
                sec_channel = (match.group("channel") or "").strip()
                sec_chat_id = (match.group("chat_id") or "").strip()

                if channel and sec_channel != channel:
                    continue
                if chat_id and sec_chat_id != chat_id:
                    continue

                # Extract user message (text between **User**: and ---)
                user_match = re.search(
                    r"\*\*User\*\*:\s*\n+(.*?)\n+---", section, re.DOTALL
                )
                if not user_match:
                    continue

                user_text = user_match.group(1).strip()
                if not user_text:
                    continue

                results.append({
                    "timestamp": f"{date_str} {sec_time}",
                    "content": user_text,
                    "channel": sec_channel,
                    "chat_id": sec_chat_id,
                    "date": date_str,
                })

                if len(results) >= limit:
                    return results

        return results

    def _truncate(self, text: str) -> str:
        if len(text) <= self._max_content_length:
            return text
        return text[: self._max_content_length] + "\n\n... [truncated]"
