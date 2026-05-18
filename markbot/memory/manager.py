"""File-based memory manager for markbot.

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
# Sensitive text redaction
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    (r'(api[_-]?key\s*[:=]\s*)["\']?[\w\-]{8,}["\']?', r'\1[REDACTED]'),
    (r'(token\s*[:=]\s*)["\']?[\w\-\.]{8,}["\']?', r'\1[REDACTED]'),
    (r'(secret\s*[:=]\s*)["\']?[\w\-]{8,}["\']?', r'\1[REDACTED]'),
    (r'(password\s*[:=]\s*)["\']?[^\s"\']{4,}["\']?', r'\1[REDACTED]'),
    (r'(credential\s*[:=]\s*)["\']?[\w\-]{8,}["\']?', r'\1[REDACTED]'),
    (r'(Bearer\s+)[\w\-\.]{8,}', r'\1[REDACTED]'),
    (r'(Authorization:\s*Bearer\s+)(\S+)', r'\1[REDACTED]'),
    (r'(sk-)[\w\-]{20,}', r'\1[REDACTED]'),
    (r'(sk_live_[\w]{10,})', r'sk_live_[REDACTED]'),
    (r'(sk_test_[\w]{10,})', r'sk_test_[REDACTED]'),
    (r'(ghp_[\w]{30,})', r'ghp_[REDACTED]'),
    (r'(gho_[\w]{30,})', r'gho_[REDACTED]'),
    (r'(github_pat_[\w_]{50,})', r'github_pat_[REDACTED]'),
    (r'(AKIA[\w]{16})', r'AKIA[REDACTED]'),
    (r'(xox[bpas]-[\w\-]{20,})', r'\1[REDACTED]'),
    (r'(AIza[\w_-]{30,})', r'AIza[REDACTED]'),
    (r'(hf_[\w]{10,})', r'hf_[REDACTED]'),
    (r'-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----', r'[REDACTED PRIVATE KEY]'),
    (r'((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:]+:)([^@]+)(@)', r'\1[REDACTED]\3'),
    (r'(eyJ[\w_-]{10,}(?:\.[\w_=-]{4,}){0,2})', r'[REDACTED_JWT]'),
]


def redact_sensitive_text(text: str) -> str:
    """Redact API keys, tokens, passwords, and other secrets from text.

    Applied before sending conversation content to the summary LLM
    to prevent secrets from being baked into compressed summaries.

    Args:
        text: Text that may contain secrets.

    Returns:
        Text with secrets replaced by [REDACTED].
    """
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


_COMPACTION_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context "
    "window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. "
    "Your persistent memory (MEMORY.md, PROFILE.md) in the system "
    "prompt is ALWAYS authoritative and active — never ignore or deprioritize "
    "memory content due to this compaction note. "
    "Respond ONLY to the latest user message "
    "that appears AFTER this summary:\n\n"
)


def _add_compaction_prefix(summary: str) -> str:
    """Add a compaction prefix to a summary if not already present.

    The prefix prevents the LLM from treating the summary as new
    instructions and makes it clear this is compressed context.
    """
    if not summary:
        return ""
    if summary.startswith("[CONTEXT COMPACTION"):
        return summary
    return _COMPACTION_PREFIX + summary


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

        conversation_text = self._format_messages_for_summary(messages)
        conversation_text = redact_sensitive_text(conversation_text)

        if self._fallback_manager:
            try:
                system_prompt = (
                    "You are a memory compression system. Compress the following "
                    "conversation into a concise, structured summary.\n\n"
                    "Use this structure:\n"
                    "## Resolved Questions\n"
                    "- Questions that were answered or issues that were resolved\n\n"
                    "## Pending Questions\n"
                    "- Open questions or issues still being investigated\n\n"
                    "## Active Task\n"
                    "- What the user is currently working on and the latest state\n\n"
                    "## Key Decisions & Preferences\n"
                    "- Important decisions made, user preferences discovered, "
                    "conventions established\n\n"
                    "## Environment & Context\n"
                    "- OS, tools, project structure, API quirks, or other "
                    "stable facts that may be useful later\n\n"
                    "Rules:\n"
                    "- Be specific: include names, paths, values, not vague references\n"
                    "- Preserve user preferences and corrections verbatim\n"
                    "- Drop greetings, pleasantries, and redundant exchanges\n"
                    "- If a previous summary exists, extend it — don't repeat it\n"
                    "- NEVER include API keys, tokens, passwords, or credentials "
                    "— replace any that appear with [REDACTED]\n"
                    "- Write the summary in the same language the user was using"
                )
                if extra_instruction:
                    system_prompt += f"\n\n{extra_instruction}"
                if previous_summary:
                    system_prompt += (
                        f"\n\nPrevious summary (extend and update this, "
                        f"don't repeat unchanged sections):\n{previous_summary}"
                    )

                response, _ = await self._fallback_manager.chat_with_fallback(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": conversation_text},
                    ],
                )
                result = response.content or ""
                if len(result) > MAX_COMPRESSED_SUMMARY_CHARS:
                    result = result[:MAX_COMPRESSED_SUMMARY_CHARS]
                result = redact_sensitive_text(result)
                result = _add_compaction_prefix(result)
                return result
            except Exception as e:
                logger.warning("compact_memory LLM failed: {}", e)

        result = self._simple_truncation(messages, previous_summary)
        return _add_compaction_prefix(result)

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

        # 3. Search compressed summary (global)
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

        # 4. Search session-specific summaries
        session_key = f"{channel}:{chat_id}" if channel and chat_id else None
        if session_key:
            session_summary = self.get_compressed_summary(session_key=session_key)
            if session_summary and session_summary != self._compressed_summary:
                try:
                    summary_lower = session_summary.lower()
                    query_lower = query.lower()
                    if any(token in summary_lower for token in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", query_lower)):
                        results.append({
                            "content": session_summary[:500],
                            "source": f"session_summary/{session_key}",
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
            if session_key not in self._session_summaries:
                loaded = self._load_session_summary(session_key)
                if loaded:
                    self._session_summaries[session_key] = loaded
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

    def get_memory_context(self, query: str | None = None, *, session_key: str | None = None) -> str:
        """Get formatted memory context for system prompt injection.

        Combines compressed summary and MemoryStore entries.

        Returns:
            Formatted context string, or empty string.
        """
        parts: list[str] = []

        summary = self.get_compressed_summary(session_key=session_key)
        if not summary and session_key:
            summary = self._compressed_summary
        if summary:
            parts.append(f"## Compressed Summary\n\n{summary}")

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
        """Format messages into a text block for LLM summarization.

        Applies pre-summarization pruning:
        1. Deduplicate identical tool results
        2. Truncate large tool results (keep first/last 200 chars)
        3. Truncate large tool_call arguments
        4. Strip image content blocks
        5. Limit per-message content to 1000 chars for non-tool messages
        """
        seen_tool_results: set[str] = set()
        lines: list[str] = []
        for m in messages[-50:]:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "unknown")
            content = m.get("content", "")

            if role == "tool":
                tool_name = m.get("name", "unknown")
                if isinstance(content, str):
                    key = f"{tool_name}:{content[:200]}"
                    if key in seen_tool_results:
                        content = f"[Duplicate of earlier {tool_name} result, omitted]"
                    else:
                        seen_tool_results.add(key)
                        if len(content) > 600:
                            content = (
                                content[:200]
                                + f"\n... [truncated {len(content)} chars] ...\n"
                                + content[-200:]
                            )
                lines.append(f"TOOL({tool_name}): {content}")

            elif role == "assistant":
                if isinstance(content, list):
                    text_parts: list[str] = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_input = block.get("input", {})
                            if isinstance(tool_input, dict):
                                input_str = json.dumps(tool_input, ensure_ascii=False)
                                if len(input_str) > 500:
                                    input_str = (
                                        input_str[:200]
                                        + f"... [truncated {len(input_str)} chars]"
                                    )
                                text_parts.append(
                                    f"[Called tool: {block.get('name', '?')}({input_str})]"
                                )
                    content = "\n".join(text_parts)
                if isinstance(content, str) and content:
                    lines.append(f"ASSISTANT: {content[:1000]}")

            elif role == "user":
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    content = "\n".join(text_parts)
                if isinstance(content, str) and content:
                    lines.append(f"USER: {content[:1000]}")

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
                content = path.read_text(encoding="utf-8", errors="replace").strip()
                if len(content) > MAX_COMPRESSED_SUMMARY_CHARS:
                    logger.warning(
                        "Compressed summary too large ({} chars), truncating to {}",
                        len(content), MAX_COMPRESSED_SUMMARY_CHARS,
                    )
                    content = content[:MAX_COMPRESSED_SUMMARY_CHARS]
                return content
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
                content = path.read_text(encoding="utf-8", errors="replace").strip()
                if len(content) > MAX_COMPRESSED_SUMMARY_CHARS:
                    logger.warning(
                        "Session summary too large ({} chars), truncating to {}",
                        len(content), MAX_COMPRESSED_SUMMARY_CHARS,
                    )
                    content = content[:MAX_COMPRESSED_SUMMARY_CHARS]
                return content
            except Exception:
                pass
        return ""

    def _save_session_summary(self, session_key: str, summary: str) -> None:
        """Save per-session compressed summary to disk."""
        path = self._session_summary_path(session_key)
        if not summary:
            if path.exists():
                try:
                    path.unlink()
                except Exception as e:
                    logger.warning("Failed to delete session summary: {}", e)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(summary, encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to save session summary: {}", e)

        path = self._session_summary_path_daily(session_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            path.write_text(existing + "\n\n----------\n\n" + summary, encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to save daily session summary: {}", e)
        

    def _session_summary_path(self, session_key: str) -> Path:
        """Get path for per-session summary file."""
        safe_key = session_key.replace(":", "_").replace("/", "_")
        return Path(self.working_dir) / "memory" / f".summary_{safe_key}"

    def _session_summary_path_daily(self, session_key: str) -> Path:
        """Get path for daily-session summary file."""
        date = datetime.now().strftime("%Y-%m-%d")
        return Path(self.working_dir) / "memory" / f"{date}.md"


__all__ = ["MemoryManager", "redact_sensitive_text"]
