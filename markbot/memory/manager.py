﻿"""File-based memory manager for markbot.

Provides a file-based memory system with:

- MemoryProvider ABC implementation for pluggability
- File-based storage (MEMORY.md, PROFILE.md, memory/daily/*.md)
- Lifecycle: prefetch, sync_turn, queue_prefetch, on_delegation
- MemoryStore for curated memory with add/replace/remove
- MemorySecurityScanner for injection/exfiltration detection
- Context fencing with <memory-context> tags
- Context compression via compact_memory()
- Keyword + daily log search via memory_search()
- Tool result offload via compact_tool_result()
- Dream-based memory optimization
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from markbot.utils.constants import (
    MAX_COMPRESSED_SUMMARY_CHARS,
    MAX_DAILY_LOG_RESULT_CHARS,
    MAX_MEMORY_MD_CHARS,
    MAX_PREFETCH_RESULTS,
    MIN_PREFETCH_SCORE,
)

from .base import BaseMemoryManager
from .daily_log import DailyLogManager
from .fencing import fence_context, sanitize_context, StreamingContextScrubber
from .provider import MemoryProvider
from .scanner import MemorySecurityScanner
from .tool import MemoryStore, MEMORY_FILENAME, USER_FILENAME

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# MemoryManager
# ---------------------------------------------------------------------------


class MemoryManager(BaseMemoryManager, MemoryProvider):
    """File-based memory manager for markbot.

    Uses MemoryStore for curated memory, DailyLogManager for
    interaction logs, and LLM-based summarization for context compression.

    Implements both BaseMemoryManager (for markbot interface compatibility)
    and MemoryProvider (for pluggability).
    """

    def __init__(
        self,
        working_dir: str,
        agent_id: str = "default",
        fallback_manager=None,
        model: str | None = None,
        embedding_config: dict | None = None,
        llm_config: dict | None = None,
        language: str = "zh",
        timezone: str | None = None,
        context_compact_enabled: bool = True,
        memory_compact_ratio: float = 0.75,
        memory_reserve_ratio: float = 0.1,
        compact_with_thinking_block: bool = True,
        memory_summary_enabled: bool = True,
        force_memory_search: bool = False,
        force_max_results: int = 1,
        force_min_score: float = 0.3,
        tool_result_compact_enabled: bool = True,
        tool_result_recent_n: int = 2,
        tool_result_old_max_bytes: int = 3000,
        tool_result_recent_max_bytes: int = 50000,
        tool_result_retention_days: int = 5,
        max_input_length: int = 131072,
    ):
        import time
        _init_start = time.time()
        logger.info("Starting initialization...")

        super().__init__(working_dir=working_dir, agent_id=agent_id)
        self._fallback_manager = fallback_manager
        self._model = model
        self._embedding_config = embedding_config or {}
        self._llm_config = llm_config or {}
        self._language = language
        self._timezone = timezone

        self.context_compact_enabled = context_compact_enabled
        self.memory_compact_ratio = memory_compact_ratio
        self.memory_reserve_ratio = memory_reserve_ratio
        self.compact_with_thinking_block = compact_with_thinking_block
        self.memory_summary_enabled = memory_summary_enabled
        self.force_memory_search = force_memory_search
        self.force_max_results = force_max_results
        self.force_min_score = force_min_score
        self.tool_result_compact_enabled = tool_result_compact_enabled
        self.tool_result_recent_n = tool_result_recent_n
        self.tool_result_old_max_bytes = tool_result_old_max_bytes
        self.tool_result_recent_max_bytes = tool_result_recent_max_bytes
        self.tool_result_retention_days = tool_result_retention_days
        self.max_input_length = max_input_length

        self._started = False
        self._compressed_summary: str = ""
        self._session_summaries: dict[str, str] = {}

        self._memory_store: MemoryStore | None = None
        self._daily_log: DailyLogManager | None = None
        self._scanner = MemorySecurityScanner()
        self._scrubber = StreamingContextScrubber()

        self._prefetch_query: str = ""
        self._prefetch_session_key: str | None = None

        # Summary toolkit (for dream optimization)
        self._summary_toolkit: Any = None

        logger.info("Initialization took {:.3f}s", time.time() - _init_start)

    # -- MemoryProvider interface -------------------------------------------

    @property
    def name(self) -> str:
        return "markbot"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session."""
        working_dir = kwargs.get("working_dir", self.working_dir)
        self._memory_store = MemoryStore(working_dir=working_dir)
        self._daily_log = DailyLogManager(workspace=Path(working_dir))
        self._compressed_summary = self._load_compressed_summary()
        logger.info("Initialized for session: {}", session_id)

    def system_prompt_block(self) -> str:
        """Return static text for the system prompt."""
        if self._memory_store:
            ctx = self._memory_store.get_memory_context()
            if ctx:
                return ctx
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn.

        Uses stored query from queue_prefetch() if available, otherwise
        searches daily logs for the given query.

        Args:
            query: The user message text.
            session_id: Optional session identifier.

        Returns:
            Formatted context string, or empty string.
        """
        search_query = self._prefetch_query or query
        if not search_query:
            return ""

        try:
            results = self._search_daily_logs(
                query=search_query,
                max_results=MAX_PREFETCH_RESULTS,
            )
            if not results:
                return ""

            lines: list[str] = []
            for r in results[:MAX_PREFETCH_RESULTS]:
                content = r.get("content", "")
                score = r.get("score", 0)
                if content and score >= MIN_PREFETCH_SCORE:
                    lines.append(f"- {content[:300]} [relevance: {score:.2f}]")

            if not lines:
                return ""

            context = "## Prefetched Memory\n\n" + "\n".join(lines)
            return fence_context(context, system_note=True)

        except Exception as e:
            logger.debug("Prefetch failed: {}", e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn.

        Stores the query so prefetch() can use it on the next call.

        Args:
            query: The user message text.
            session_id: Optional session identifier.
        """
        if not query:
            return
        self._prefetch_query = query
        self._prefetch_session_key = session_id

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """Persist a completed turn."""
        if self._daily_log:
            channel = ""
            chat_id = ""
            if session_id and ":" in session_id:
                channel, chat_id = session_id.split(":", 1)
            self._daily_log.append_turn(
                user_content=user_content,
                assistant_content=assistant_content,
                channel=channel,
                chat_id=chat_id,
            )

    def shutdown(self) -> None:
        """Clean up resources."""
        self._save_compressed_summary(self._compressed_summary)
        self._started = False
        logger.info("Shutdown complete")

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """Mirror built-in memory writes.

        When the memory tool writes to MEMORY.md or PROFILE.md,
        this hook syncs the change to the daily log for searchability.

        Args:
            action: 'add', 'replace', or 'remove'.
            target: 'memory' or 'user'.
            content: The entry content.
            metadata: Optional provenance metadata.
        """
        if self._daily_log:
            self._daily_log.append_turn(
                user_content=f"[Memory {action}:{target}]",
                assistant_content=content[:500],
                channel="memory_tool",
                chat_id=action,
            )

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs,
    ) -> None:
        """Called on the PARENT agent when a subagent completes."""
        if self._daily_log:
            summary = f"[Delegation] Task: {task[:200]}... Result: {result[:500]}..."
            self._daily_log.append_turn(
                user_content=f"[Subagent {child_session_id}] Delegated task",
                assistant_content=summary,
                channel="subagent",
                chat_id=child_session_id,
            )

    # -- BaseMemoryManager interface (markbot compatibility) -----------------

    async def start(self) -> None:
        """Start the memory manager."""
        if self._started:
            return
        self._memory_store = MemoryStore(working_dir=self.working_dir)
        self._daily_log = DailyLogManager(workspace=Path(self.working_dir))
        self._compressed_summary = self._load_compressed_summary()
        self._started = True
        logger.info("Started")

    async def close(self) -> bool:
        """Close and cleanup."""
        self._save_compressed_summary(self._compressed_summary)
        self._started = False
        logger.info("Closed")
        return True

    async def compact_tool_result(self, **kwargs) -> None:
        """Compact tool results by truncating large outputs.

        Keeps the last N tool results intact, truncates older ones.
        """
        if not self.tool_result_compact_enabled:
            return
        messages = kwargs.get("messages", [])
        if not messages:
            return

        recent_n = self.tool_result_recent_n
        old_max_bytes = self.tool_result_old_max_bytes
        recent_max_bytes = self.tool_result_recent_max_bytes

        # Find tool result indices
        tool_indices = [
            i for i, m in enumerate(messages)
            if isinstance(m, dict) and m.get("role") == "tool"
        ]

        # Keep recent N intact
        keep_indices = set(tool_indices[-recent_n:]) if recent_n > 0 else set()

        for i, m in enumerate(messages):
            if not isinstance(m, dict) or m.get("role") != "tool":
                continue
            content = m.get("content", "")
            if not isinstance(content, str):
                continue

            max_bytes = recent_max_bytes if i in keep_indices else old_max_bytes
            if len(content) > max_bytes:
                m["content"] = content[:max_bytes] + "\n\n... [truncated by compact_tool_result]"

    async def check_context(self, **kwargs) -> tuple:
        """Check context size and determine if compaction is needed.

        Returns:
            Tuple of (messages_to_compact, remaining_messages, is_valid).
        """
        messages = kwargs.get("messages", [])
        if not messages:
            return [], [], True

        # Calculate total context size
        total_chars = sum(
            len(m.get("content", "")) if isinstance(m.get("content", ""), str) else 0
            for m in messages
        )

        # Add system prompt size
        system_prompt = kwargs.get("system_prompt", "")
        total_chars += len(system_prompt)

        threshold = self.max_input_length * self.memory_compact_ratio
        reserve = int(self.max_input_length * self.memory_reserve_ratio)

        if total_chars <= threshold:
            return [], messages, True

        # Determine how many messages to compact
        compact_count = int(len(messages) * (1 - self.memory_reserve_ratio))
        messages_to_compact = messages[:compact_count]
        remaining = messages[compact_count:]

        return messages_to_compact, remaining, False

    async def compact_memory(
        self,
        messages: list,
        previous_summary: str = "",
        extra_instruction: str = "",
        **kwargs,
    ) -> str:
        """Compact messages into a condensed summary.

        Uses LLM if available, otherwise simple truncation.

        Returns:
            Condensed summary string, or empty string on failure.
        """
        if not messages:
            return previous_summary or ""

        # Format messages for summarization
        conversation_text = self._format_messages_for_summary(messages)

        if self._fallback_manager:
            try:
                system_prompt = (
                    "You are a memory compression system. Compress the following "
                    "conversation into a concise summary. Preserve key decisions, "
                    "preferences, facts, and action items. Be specific."
                )
                if extra_instruction:
                    system_prompt += f"\n\n{extra_instruction}"
                if previous_summary:
                    system_prompt += f"\n\nPrevious summary (extend this):\n{previous_summary}"

                response, _ = await self._fallback_manager.chat_with_fallback(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": conversation_text},
                    ],
                )
                result = response.content or ""
                if len(result) > MAX_COMPRESSED_SUMMARY_CHARS:
                    result = result[:MAX_COMPRESSED_SUMMARY_CHARS]
                return result
            except Exception as e:
                logger.warning("compact_memory LLM failed: {}", e)

        # Simple truncation fallback
        return self._simple_truncation(messages, previous_summary)

    async def summary_memory(self, messages: list, **kwargs) -> str:
        """Generate a comprehensive summary and write to MEMORY.md.

        Uses LLM if available, then appends to MemoryStore.

        Returns:
            Summary string.
        """
        if not messages:
            return ""

        conversation_text = self._format_messages_for_summary(messages)

        if self._fallback_manager:
            try:
                system_prompt = (
                    "Generate a comprehensive summary of this conversation. "
                    "Extract: user preferences, project decisions, technical details, "
                    "environment facts, and action items. Format as concise bullet points."
                )
                response, _ = await self._fallback_manager.chat_with_fallback(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": conversation_text},
                    ],
                )
                summary = response.content or ""

                # Write to MEMORY.md via MemoryStore
                if summary and self._memory_store:
                    try:
                        self._memory_store.add("memory", summary)
                    except Exception as e:
                        logger.warning("Failed to write summary to MemoryStore: {}", e)

                return summary
            except Exception as e:
                logger.warning("summary_memory LLM failed: {}", e)

        return self._simple_truncation(messages, "")

    async def memory_search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> Any:
        """Search stored memories for relevant content.

        Searches daily logs by keyword. Returns list of dicts with
        content, source, score keys.

        Args:
            query: Search query string.
            max_results: Maximum number of results.
            min_score: Minimum relevance score.
            channel: Optional channel filter.
            chat_id: Optional chat ID filter.

        Returns:
            List of result dicts, or empty list.
        """
        results: list[dict] = []

        # 1. Search daily logs
        if self._daily_log:
            try:
                log_results = self._daily_log.search(
                    query=query,
                    max_results=max_results,
                    channel=channel,
                    chat_id=chat_id,
                )
                results.extend(log_results)
            except Exception as e:
                logger.debug("Daily log search failed: {}", e)

        # 2. Search MemoryStore entries
        if self._memory_store:
            try:
                query_lower = query.lower()
                query_tokens = set(re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", query_lower))

                for target, entries in [
                    ("memory", self._memory_store.memory_entries),
                    ("user", self._memory_store.user_entries),
                ]:
                    for entry in entries:
                        entry_lower = entry.lower()
                        entry_tokens = set(re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", entry_lower))
                        hits = sum(1 for t in query_tokens if t in entry_tokens)
                        if hits > 0 and query_tokens:
                            score = hits / len(query_tokens)
                            if score >= min_score:
                                results.append({
                                    "content": entry[:500],
                                    "source": f"{target}/{MEMORY_FILENAME if target == 'memory' else USER_FILENAME}",
                                    "score": round(score, 3),
                                })
            except Exception as e:
                logger.debug("MemoryStore search failed: {}", e)

        # 3. Search compressed summary
        if self._compressed_summary:
            try:
                summary_lower = self._compressed_summary.lower()
                query_lower = query.lower()
                if any(token in summary_lower for token in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", query_lower)):
                    results.append({
                        "content": self._compressed_summary[:500],
                        "source": "compressed_summary",
                        "score": 0.5,
                    })
            except Exception:
                pass

        # Deduplicate by content and sort by score descending
        seen_contents: set[str] = set()
        unique_results: list[dict] = []
        for r in sorted(results, key=lambda x: -x.get("score", 0)):
            content = r.get("content", "")
            if content not in seen_contents:
                seen_contents.add(content)
                unique_results.append(r)
                if len(unique_results) >= max_results:
                    break

        return unique_results

    # -- Extra methods for tool compatibility --------------------------------

    async def add_memory(self, content: str, tags: list[str] | None = None) -> bool:
        """Add a memory entry (for MemorySaveTool compatibility).

        Args:
            content: The memory content to save.
            tags: Optional categorization tags.

        Returns:
            True if successful.
        """
        if not self._memory_store:
            return False
        result = self._memory_store.add("memory", content)
        return result.get("success", False)

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory entry (for MemoryForgetTool compatibility).

        Args:
            memory_id: ID or text identifying the entry to delete.

        Returns:
            True if successful.
        """
        if not self._memory_store:
            return False
        result = self._memory_store.remove("memory", memory_id)
        return result.get("success", False)

    async def list_memories(self, limit: int = 20) -> list[dict]:
        """List recent memory entries (for MemoryListTool compatibility).

        Args:
            limit: Maximum number of entries.

        Returns:
            List of dicts with id, content, tags keys.
        """
        if not self._memory_store:
            return []
        result = self._memory_store.read("memory")
        entries = result.get("entries", [])
        return [
            {
                "id": str(i),
                "content": entry,
                "tags": [],
            }
            for i, entry in enumerate(entries[:limit])
        ]

    async def dream(self, **kwargs) -> str:
        """Run dream-based memory optimization.

        Uses LLM to consolidate MEMORY.md entries.

        Returns:
            Status message.
        """
        if not self._memory_store:
            return "Memory store not available"

        memory_path = Path(self.working_dir) / MEMORY_FILENAME
        if not memory_path.exists():
            return "No MEMORY.md to optimize"

        # Create backup
        backup_dir = Path(self.working_dir) / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"memory_backup_{timestamp}.md"

        try:
            import shutil
            shutil.copyfile(memory_path, backup_path)
        except Exception as e:
            return f"Failed to create backup: {e}"

        if not self._fallback_manager:
            return "No LLM available for dream optimization"

        try:
            content = memory_path.read_text(encoding="utf-8", errors="replace")
            response, _ = await self._fallback_manager.chat_with_fallback(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a Dream Memory Organizer. Read the MEMORY.md content, "
                            "deduplicate entries, merge related items, remove outdated info, "
                            "and reorganize into a clean markdown format. "
                            "Return ONLY the optimized markdown content."
                        ),
                    },
                    {"role": "user", "content": content},
                ],
            )
            optimized = response.content or ""
            if optimized:
                memory_path.write_text(optimized, encoding="utf-8")
                # Reload MemoryStore
                self._memory_store = MemoryStore(working_dir=self.working_dir)
                return f"Dream optimization completed. Backup at {backup_path}"
            return "Dream produced empty result"
        except Exception as e:
            return f"Dream optimization failed: {e}"

    def get_in_memory_memory(self, **kwargs) -> Any:
        """Retrieve the in-memory memory object (stub for compatibility)."""
        return None

    def get_compressed_summary(self, *, session_key: str | None = None) -> str:
        """Return the current compressed summary string."""
        if session_key:
            return self._session_summaries.get(session_key, "")
        return self._compressed_summary

    def set_compressed_summary(
        self,
        summary: str,
        *,
        session_key: str | None = None,
    ) -> None:
        """Update the compressed summary string."""
        if session_key:
            self._session_summaries[session_key] = summary
            self._save_session_summary(session_key, summary)
        else:
            self._compressed_summary = summary
            self._save_compressed_summary(summary)

    def get_memory_context(self, query: str | None = None) -> str:
        """Get formatted memory context for system prompt injection.

        Combines compressed summary and MemoryStore entries.

        Returns:
            Formatted context string, or empty string.
        """
        parts: list[str] = []

        # 1. Compressed summary
        if self._compressed_summary:
            parts.append(f"## Compressed Summary\n\n{self._compressed_summary}")

        # 2. MemoryStore entries
        if self._memory_store:
            memory_ctx = self._memory_store.get_memory_context(query=query)
            if memory_ctx:
                parts.append(memory_ctx)

        if not parts:
            return ""

        return "\n\n".join(parts)

    async def restart_embedding_model(self) -> None:
        """Restart the embedding model (no-op for file-based backend)."""
        logger.debug("restart_embedding_model: no-op for file backend")

    # -- Internal helpers ---------------------------------------------------

    def _format_messages_for_summary(self, messages: list) -> str:
        """Format messages into a text block for LLM summarization."""
        lines: list[str] = []
        for m in messages[-50:]:  # Last 50 messages max
            if not isinstance(m, dict):
                continue
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                content = "\n".join(text_parts)
            if isinstance(content, str) and content:
                lines.append(f"{role.upper()}: {content[:1000]}")
        return "\n\n".join(lines)

    def _simple_truncation(self, messages: list, previous_summary: str) -> str:
        """Simple truncation-based fallback summary."""
        parts: list[str] = []
        if previous_summary:
            parts.append(f"Previous summary: {previous_summary[:500]}")

        user_msgs = []
        for m in messages[-20:]:
            if isinstance(m, dict) and m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str) and content:
                    user_msgs.append(content[:200])

        if user_msgs:
            parts.append("Recent user messages:")
            for msg in user_msgs[-10:]:
                parts.append(f"- {msg}")

        return "\n".join(parts)

    def _search_daily_logs(
        self,
        query: str,
        max_results: int = 5,
    ) -> list[dict]:
        """Search daily log files by keyword."""
        if not self._daily_log:
            return []
        return self._daily_log.search(query=query, max_results=max_results)

    # -- Compressed summary persistence -------------------------------------

    def _load_compressed_summary(self) -> str:
        """Load compressed summary from disk."""
        path = Path(self.working_dir) / "memory" / ".compressed_summary"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                pass
        return ""

    def _save_compressed_summary(self, summary: str) -> None:
        """Save compressed summary to disk."""
        if not summary:
            return
        path = Path(self.working_dir) / "memory" / ".compressed_summary"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(summary, encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to save compressed summary: {}", e)

    def _load_session_summary(self, session_key: str) -> str:
        """Load per-session compressed summary from disk."""
        path = self._session_summary_path(session_key)
        if path.exists():
            try:
                return path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                pass
        return ""

    def _save_session_summary(self, session_key: str, summary: str) -> None:
        """Save per-session compressed summary to disk."""
        if not summary:
            return
        path = self._session_summary_path(session_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(summary, encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to save session summary: {}", e)

    def _session_summary_path(self, session_key: str) -> Path:
        """Get path for per-session summary file."""
        safe_key = session_key.replace(":", "_").replace("/", "_")
        return Path(self.working_dir) / "memory" / f".summary_{safe_key}"


__all__ = ["MemoryManager"]
