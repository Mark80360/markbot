"""Tiered Memory Manager - Central controller for all memory layers."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from loguru import logger

from .whiteboard import WhiteboardMemory
from .hot_memory import HotMemory
from .warm_memory import WarmMemory
from .cold_memory import ColdMemory
from .base import CompactResult

if TYPE_CHECKING:
    from markbot.session.manager import Session


@dataclass
class MemoryContext:
    """Assembled memory context for prompt injection."""
    session_context: str = ""
    hot_context: str = ""  
    warm_context: str = ""
    cold_context: str = ""
    whiteboard_context: str = ""
    
    def to_prompt(self) -> str:
        """Format all context for prompt."""
        parts = []
        
        if self.whiteboard_context:
            parts.append(self.whiteboard_context)
        
        if self.session_context:
            parts.append(self.session_context)
        
        if self.hot_context:
            parts.append(self.hot_context)
        
        if self.warm_context:
            parts.append(self.warm_context)
            
        if self.cold_context:
            parts.append(self.cold_context)
        
        return "\n\n".join(parts)
    
    def is_empty(self) -> bool:
        """Check if context is empty."""
        return not any([
            self.session_context,
            self.hot_context,
            self.warm_context,
            self.cold_context,
            self.whiteboard_context
        ])


class TieredMemoryManager:
    """
    Central manager for L1-L4 tiered memory system.
    
    Coordinates memory flow:
    - L1 Whiteboard: Loop-level temporary (cleared after loop)
    - L1.5 Session: Chat-level sliding window (8 turns)
    - L2 Hot: Global important info (20 items max)
    - L3 Warm: Daily activity logs (30 days TTL)
    - L4 Cold: Semantic long-term storage (vector DB)
    """
    
    # Configuration defaults
    DEFAULT_SESSION_WINDOW = 20  # L1.5: compaction后保留最近20条消息
    DEFAULT_HOT_CAPACITY = 30   # L2: Max 30 items
    DEFAULT_WARM_TTL_DAYS = 30  # L3: 30 days retention
    COMPACT_THRESHOLD = 35      # 超过35轮时触发compaction（必须 > DEFAULT_SESSION_WINDOW）
    
    def __init__(self, workspace_path: str, enable_cold: bool = True,
                 session_window: int | None = None,
                 hot_capacity: int | None = None,
                 warm_ttl_days: int | None = None,
                 compact_threshold: int | None = None):
        """
        Initialize tiered memory manager.

        Args:
            workspace_path: Base workspace directory (memory files go to workspace/memory/)
            enable_cold: Whether to enable L4 cold memory (requires chromadb)
            session_window: Override DEFAULT_SESSION_WINDOW
            hot_capacity: Override DEFAULT_HOT_CAPACITY
            warm_ttl_days: Override DEFAULT_WARM_TTL_DAYS
            compact_threshold: Override COMPACT_THRESHOLD
        """
        self.workspace_path = workspace_path

        # Apply config overrides (use provided values or class defaults)
        self.SESSION_WINDOW = session_window or self.DEFAULT_SESSION_WINDOW
        self.HOT_CAPACITY = hot_capacity or self.DEFAULT_HOT_CAPACITY
        self.WARM_TTL_DAYS = warm_ttl_days or self.DEFAULT_WARM_TTL_DAYS
        self.COMPACT_THRESHOLD = compact_threshold or self.COMPACT_THRESHOLD

        if self.COMPACT_THRESHOLD <= self.SESSION_WINDOW:
            logger.warning(
                f"compact_threshold({self.COMPACT_THRESHOLD}) <= session_window("
                f"{self.SESSION_WINDOW}), compaction will never trigger. "
                f"Auto-adjusting threshold to {self.SESSION_WINDOW + 15}"
            )
            self.COMPACT_THRESHOLD = self.SESSION_WINDOW + 15

        # Ensure memory directory exists
        self.memory_dir = Path(workspace_path) / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # Initialize L2 and L3 first (they're global/session-agnostic)
        self.hot = HotMemory(workspace_path, max_entries=self.HOT_CAPACITY)
        self.warm = WarmMemory(workspace_path, ttl_days=self.WARM_TTL_DAYS)

        # L4 is optional based on dependencies
        self.cold = None
        if enable_cold:
            try:
                self.cold = ColdMemory(workspace_path)
            except Exception as e:
                logger.warning(f"Failed to initialize ColdMemory: {e}")

        # L1 Whiteboards are per-chat, stored in dict
        self._whiteboards: Dict[str, WhiteboardMemory] = {}

        # Tracking for compaction
        self._session_turn_counts: Dict[str, int] = {}

        logger.info(
            f"TieredMemoryManager initialized at {workspace_path} "
            f"(window={self.SESSION_WINDOW}, hot={self.HOT_CAPACITY}, "
            f"warm_ttl={self.WARM_TTL_DAYS}d, compact_at={self.COMPACT_THRESHOLD})"
        )
        self._load_turn_counts()
    
    @property
    def cold_available(self) -> bool:
        """Check if cold memory is available and healthy."""
        return self.cold is not None and hasattr(self.cold, 'is_available') and self.cold.is_available()

    def _get_turn_counts_path(self) -> Path:
        return self.memory_dir / "turn_counts.json"

    def _load_turn_counts(self) -> None:
        """Load turn counts from disk for restart safety."""
        path = self._get_turn_counts_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._session_turn_counts = {k: int(v) for k, v in data.items()}
                logger.info(f"[Memory] Loaded turn counts for {len(self._session_turn_counts)} sessions")
            except Exception as e:
                logger.warning(f"[Memory] Failed to load turn counts: {e}")

    def _persist_turn_counts(self) -> None:
        """Persist turn counts to disk."""
        try:
            path = self._get_turn_counts_path()
            path.write_text(json.dumps(self._session_turn_counts), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[Memory] Failed to persist turn counts: {e}")
    
    def get_whiteboard(self, chat_id: str) -> WhiteboardMemory:
        """Get or create whiteboard for a chat session."""
        if chat_id not in self._whiteboards:
            self._whiteboards[chat_id] = WhiteboardMemory(self.workspace_path, chat_id)
        return self._whiteboards[chat_id]
    
    def start_loop(self, chat_id: str) -> WhiteboardMemory:
        """
        Start an Agent Loop for a chat session.
        
        - Restores checkpoint if exists
        - Initializes task frame
        - Increments loop counter
        """
        wb = self.get_whiteboard(chat_id)
        
        # Try to restore checkpoint
        if wb.load_checkpoint():
            logger.info(f"[Loop Start] Restored checkpoint for {chat_id}")
        else:
            logger.info(f"[Loop Start] New whiteboard for {chat_id}")
            wb.ensure_task_frame()
        
        # Update loop counter
        loop_count = wb.get("loop_counter", 0)
        wb.update("loop_counter", loop_count + 1)
        wb.update("current_state", "PLANNING")
        
        return wb
    
    def end_loop(self, chat_id: str, success: bool = True) -> None:
        """
        End an Agent Loop for a chat session.
        
        - Clears whiteboard (temporary by design)
        - Removes checkpoint
        Updates session metadata
        """
        wb = self.get_whiteboard(chat_id)
        
        # Save final checkpoint if needed
        if not success:
            wb.save_checkpoint()
            logger.info(f"[Loop End] Checkpoint saved for {chat_id}")
        else:
            wb.clear_checkpoint()
        
        # Clear whiteboard
        wb.clear()
        
        # Remove from active dict
        if chat_id in self._whiteboards:
            del self._whiteboards[chat_id]
        
        logger.info(f"[Loop End] Cleared whiteboard for {chat_id}")
    
    def _extract_turn_facts(self, user_input: str, assistant_response: str) -> str | None:
        """从每轮对话中提取关键事实，支持中英文。

        基于规则提取文件操作、决策、完成状态、错误修复等关键信息。
        """
        facts = []

        file_ops = re.findall(
            r'(创建|修改|编辑|删除|写入|新增|重命名|移动)\s*[`\"]?(.+?\.\w+)[`\"]?',
            assistant_response
        )
        for op, path in file_ops:
            facts.append(f"文件操作: {op} {path}")

        file_ops_en = re.findall(
            r'(created|modified|edited|deleted|written|added|renamed|moved)\s+[`\"]?(.+?\.\w+)[`\"]?',
            assistant_response,
            re.IGNORECASE
        )
        for op, path in file_ops_en:
            facts.append(f"File operation: {op} {path}")

        decision_patterns = [
            (r'(决定|选择|采用|使用|实现|改为|切换).{0,80}', '决策'),
            (r'(decided|chose|implemented|used|switched to).{0,80}', 'Decision'),
            (r'(完成|finished|done|已).{0,40}(功能|feature|任务|task|实现)', '完成'),
            (r'(错误|error|失败|failed|异常|exception).{0,60}(修复|fix|解决|resolved|处理|handled)', '修复'),
            (r'(注意|note|提醒|warning|warn).{0,60}', '注意'),
            (r'(用户要求|用户希望|用户想要|user requested|user wants).{0,60}', '用户需求'),
            (r'(下一步|next step|接下来|then|之后).{0,60}', '后续计划'),
        ]
        for pattern, label in decision_patterns:
            matches = re.findall(pattern, assistant_response, re.IGNORECASE)
            for m in matches[:2]:
                text = m if isinstance(m, str) else m[0]
                facts.append(f"[{label}] {text.strip()}")

        return "\n".join(facts) if facts else None

    def save_turn(self, chat_id: str, user_input: str, assistant_response: str,
                  session: Optional[Session] = None,
                  metadata: Optional[Dict[str, Any]] = None) -> Optional[CompactResult]:
        """
        Save a completed turn to all relevant memory layers.

        Args:
            chat_id: Chat identifier
            user_input: User's message
            assistant_response: Assistant's response
            session: Optional Session object for L1.5 storage
            metadata: Additional metadata

        Returns:
            CompactResult if compaction was triggered
        """
        result = None

        # L3: Always log to warm memory (完整日志)
        self.warm.add_turn(chat_id, user_input, assistant_response, metadata)

        # L2: Extract key facts from this turn (每轮提取关键事实)
        turn_facts = self._extract_turn_facts(user_input, assistant_response)
        if turn_facts:
            self.hot.add_important(turn_facts, category="TurnFact")

        # L4: Periodic archiving to cold memory (每5轮归档一次)
        # Use session message count as ground truth for restart safety
        current_count = self._session_turn_counts.get(chat_id, 0) + 1
        self._session_turn_counts[chat_id] = current_count
        self._persist_turn_counts()
        if self.cold_available and current_count % 5 == 0:
            try:
                turn_doc = (
                    f"[{chat_id}] Turn #{current_count}\n"
                    f"User: {user_input[:500]}\n"
                    f"Assistant: {assistant_response[:500]}"
                )
                self.cold.add_document(turn_doc, collection="conversations")
            except Exception as e:
                logger.warning(f"[Memory] Cold archive failed: {e}")

        # L1.5: Compaction check — use session.messages length as authoritative source
        if session:
            msg_count = len(session.messages)
            if msg_count > self.COMPACT_THRESHOLD:
                logger.info(
                    f"[Memory] Triggering compaction for {chat_id} "
                    f"(messages={msg_count}, threshold={self.COMPACT_THRESHOLD})"
                )
                result = self._compact_session(session)
                self._session_turn_counts[chat_id] = self.SESSION_WINDOW
                self._persist_turn_counts()

        return result
    
    def _compact_session(self, session: Session) -> CompactResult:
        """
        Compact session memory by extracting key facts to Hot memory.

        Uses rule-based extraction (supports Chinese + English) to extract
        structured summary from archived messages, then writes to Hot/Warm/Cold.
        Keeps recent DEFAULT_SESSION_WINDOW turns, archives older ones.
        """
        chat_id = session.key
        all_messages = session.messages

        if len(all_messages) <= self.SESSION_WINDOW:
            return CompactResult([], len(all_messages), chat_id)

        # Messages to archive
        to_archive = all_messages[:-self.SESSION_WINDOW]
        to_keep = all_messages[-self.SESSION_WINDOW:]

        # Build structured summary from archived messages
        summary_parts = []
        decisions = []
        file_ops = []
        errors_fixed = []
        user_intents = []

        for msg in to_archive:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content or role not in ("user", "assistant"):
                continue

            prefix = "[用户]" if role == "user" else "[助手]"

            # Extract file operations
            ops = re.findall(
                r'(创建|修改|编辑|删除|写入|新增|重命名)\s*[`\"]?(.+?\.\w+)[`\"]?',
                content
            )
            for op, path in ops:
                file_ops.append(f"{prefix} {op}: {path}")

            ops_en = re.findall(
                r'(created|modified|edited|deleted|written|added|renamed)\s+[`\"]?(.+?\.\w+)[`\"]?',
                content,
                re.IGNORECASE
            )
            for op, path in ops_en:
                file_ops.append(f"{prefix} {op}: {path}")

            # Extract decisions (中英文)
            decision_cn = re.findall(
                r'(决定|选择|采用|使用|实现|改为).{0,80}', content
            )
            for d in decision_cn[:2]:
                decisions.append(f"{prefix} {d.strip()}")

            decision_en = re.findall(
                r'(decided|chose|implemented|used|switched to).{0,80}',
                content,
                re.IGNORECASE
            )
            for d in decision_en[:2]:
                decisions.append(f"{prefix} {d.strip()}")

            # Extract error fixes
            fixes = re.findall(
                r'(错误|error|失败|failed|异常|exception).{0,60}(修复|fix|解决|resolved|处理)',
                content,
                re.IGNORECASE
            )
            for fix in fixes[:2]:
                text = fix if isinstance(fix, str) else ' '.join(fix)
                errors_fixed.append(f"{prefix} {text.strip()}")

            # Extract user intents from user messages
            if role == "user":
                intent_patterns = re.findall(
                    r'(帮我|请|需要|想要|希望|能不能|可以|help|please|need|want|can you).{0,60}',
                    content,
                    re.IGNORECASE
                )
                for intent in intent_patterns[:2]:
                    text = intent if isinstance(intent, str) else ' '.join(intent)
                    user_intents.append(text.strip())

        # Build the structured summary
        if user_intents:
            summary_parts.append("## 用户需求\n" + "\n".join(f"- {i}" for i in user_intents[-5:]))
        if decisions:
            summary_parts.append("## 关键决策\n" + "\n".join(f"- {d}" for d in decisions[-8:]))
        if file_ops:
            summary_parts.append("## 文件操作\n" + "\n".join(f"- {f}" for f in file_ops[-10:]))
        if errors_fixed:
            summary_parts.append("## 问题与修复\n" + "\n".join(f"- {e}" for e in errors_fixed[-5:]))

        if summary_parts:
            full_summary = f"[Session Compact: {chat_id}] 归档 {len(to_archive)} 条消息\n\n" + \
                           "\n\n".join(summary_parts)

            # Write to Hot Memory as structured summary
            self.hot.add_important(full_summary, category="SessionSummary")

            # Write to Warm as compaction record
            self.warm.add_event(chat_id, "system",
                                f"[COMPACTED] Archived {len(to_archive)} messages, "
                                f"kept {len(to_keep)}. Summary:\n{full_summary}")

            # Write to Cold if available
            if self.cold_available:
                try:
                    self.cold.add_document(full_summary, collection="session_summaries")
                except Exception as e:
                    logger.warning(f"[Compact] Cold write failed: {e}")

            logger.info(f"[Compact] Structured summary generated with "
                        f"{len(decisions)} decisions, {len(file_ops)} file ops, "
                        f"{len(errors_fixed)} fixes")
        else:
            logger.warning(f"[Compact] No extractable facts from {len(to_archive)} messages")

        # Update session: truncate to recent window
        session.messages = to_keep
        session.last_consolidated = 0

        archived = [{"role": m.get("role"), "content": m.get("content", "")[:200]}
                   for m in to_archive]

        result = CompactResult(archived, len(to_keep), chat_id)
        logger.info(f"[Compact] Archived {len(to_archive)} messages, kept {len(to_keep)}")

        return result
    
    def add_hot_fact(self, content: str, category: str = "General") -> None:
        """Add an important fact to Hot memory."""
        self.hot.add_important(content, category)
    
    def add_todo(self, item: str) -> None:
        """Add an item to Hot memory todo list."""
        self.hot.append_todo(item)
    
    def complete_todo(self, item: str) -> bool:
        """Mark a todo as complete."""
        return self.hot.complete_todo(item)
    
    def assemble_context(self, chat_id: str, user_input: str,
                        session: Optional[Session] = None,
                        include_whiteboard: bool = True) -> MemoryContext:
        """
        Assemble memory context from all layers for prompt injection.

        Note: Session raw history is NOT included here because it is already
        passed separately via the 'history' parameter in build_messages().
        This method focuses on extracted/summarized context from other layers.

        Priority order:
        1. Whiteboard (L1 - current loop state)
        2. Hot (L2 - global important facts + compaction summaries)
        3. Warm (L3 - recent activity logs, filtered by chat_id)
        4. Cold (L4 - semantic search results)
        """
        context = MemoryContext()

        # L1: Whiteboard (if loop is active)
        if include_whiteboard and chat_id in self._whiteboards:
            context.whiteboard_context = self._whiteboards[chat_id].get_context()

        # L2: Hot memory (extracted facts + compaction summaries)
        context.hot_context = self.hot.get_context()

        # L3: Warm memory (recent activity, filtered by chat_id)
        context.warm_context = self.warm.get_context(limit=3, chat_id=chat_id)

        # L4: Cold memory (semantic search on user_input)
        if self.cold and self.cold.is_available():
            context.cold_context = self.cold.get_context(query=user_input, limit=3)

        return context
    
    def search_cold_memory(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Search cold memory semantically."""
        if self.cold:
            return self.cold.search(query, limit=limit)
        return []
    
    async def maintenance(self) -> Dict[str, Any]:
        """
        Periodic maintenance tasks:
        - Cleanup expired warm memory
        - Compact hot memory if needed
        - Archive to cold memory
        
        Should be called periodically (e.g., every 30 minutes).
        """
        results = {
            "warm_cleaned": 0,
            "cold_archived": 0,
            "errors": []
        }
        
        # Cleanup warm memory
        try:
            removed = self.warm.cleanup_expired()
            results["warm_cleaned"] = len(removed)
            if removed:
                logger.info(f"[Maintenance] Cleaned {len(removed)} expired warm files")
        except Exception as e:
            results["errors"].append(f"Warm cleanup: {e}")
        
        # Compact to cold memory (last day's warm entries)
        if self.cold and self.cold.is_available():
            try:
                # Get yesterday's entries
                import datetime
                yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                warm_content = self.warm.read_date(yesterday)
                
                if warm_content:
                    # Simple split into entries
                    entries = warm_content.split("\n## ")[1:]  # Skip header
                    parsed_entries = []
                    for entry in entries[:20]:  # Limit to 20
                        lines = entry.split("\n")
                        content = "\n".join(lines[1:]) if len(lines) > 1 else entry
                        parsed_entries.append({"content": content})
                    
                    # Archive to cold
                    archived_count = self.cold.compact_from_hot_and_warm([], parsed_entries)
                    results["cold_archived"] = archived_count
                    
                    if archived_count:
                        logger.info(f"[Maintenance] Archived {archived_count} entries to cold memory")
            except Exception as e:
                results["errors"].append(f"Cold archive: {e}")
        
        return results
    
    def get_stats(self) -> Dict[str, Any]:
        """Get memory system statistics."""
        stats = {
            "hot_entries": 0,
            "cold_documents": 0,
            "active_whiteboards": len(self._whiteboards),
            "cold_available": self.cold is not None and self.cold.is_available()
        }
        
        # Count hot entries
        try:
            content = self.hot.read()
            stats["hot_entries"] = content.count("\n- [")
        except Exception:
            pass
        
        # Count cold documents
        if self.cold:
            try:
                cold_stats = self.cold.get_collection_stats()
                stats["cold_documents"] = cold_stats.get("total_documents", 0)
            except Exception:
                pass
        
        return stats
    
    def clear_all(self, confirm: bool = False) -> None:
        """
        Clear all memory layers (use with extreme caution!).
        
        Requires confirm=True to prevent accidental data loss.
        """
        if not confirm:
            logger.warning("Clear all memory requires confirm=True")
            return
        
        # Clear whiteboards
        for chat_id in list(self._whiteboards.keys()):
            self._whiteboards[chat_id].clear()
        self._whiteboards.clear()
        
        # Clear other layers
        self.hot.clear()
        self.warm.clear()
        if self.cold:
            self.cold.clear()
        
        logger.warning("All memory layers cleared!")
