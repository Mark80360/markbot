"""Tiered Memory Manager - Central controller for all memory layers."""

from __future__ import annotations

import asyncio
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
    DEFAULT_SESSION_WINDOW = 8  # L1.5: 8-turn sliding window
    DEFAULT_HOT_CAPACITY = 20   # L2: Max 20 items
    DEFAULT_WARM_TTL_DAYS = 30  # L3: 30 days retention
    COMPACT_THRESHOLD = 8       # Trigger compact at 8 turns
    
    def __init__(self, workspace_path: str, enable_cold: bool = True):
        """
        Initialize tiered memory manager.
        
        Args:
            workspace_path: Base workspace directory (memory files go to workspace/memory/)
            enable_cold: Whether to enable L4 cold memory (requires chromadb)
        """
        self.workspace_path = workspace_path
        
        # Ensure memory directory exists
        self.memory_dir = Path(workspace_path) / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize L2 and L3 first (they're global/session-agnostic)
        self.hot = HotMemory(workspace_path, max_entries=self.DEFAULT_HOT_CAPACITY)
        self.warm = WarmMemory(workspace_path, ttl_days=self.DEFAULT_WARM_TTL_DAYS)
        
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
        
        logger.info(f"TieredMemoryManager initialized at {workspace_path}")
    
    @property
    def cold_available(self) -> bool:
        """Check if cold memory is available and healthy."""
        return self.cold is not None and hasattr(self.cold, 'is_available') and self.cold.is_available()
    
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
        
        # L3: Always log to warm memory
        self.warm.add_turn(chat_id, user_input, assistant_response, metadata)
        
        # L1.5: Update session if provided
        if session:
            # Track turn count
            current_count = self._session_turn_counts.get(chat_id, 0) + 1
            self._session_turn_counts[chat_id] = current_count
            
            # Check if compaction needed
            if current_count > self.COMPACT_THRESHOLD:
                logger.info(f"[Memory] Triggering compaction for {chat_id} ({current_count} turns)")
                result = self._compact_session(session)
                self._session_turn_counts[chat_id] = self.DEFAULT_SESSION_WINDOW
        
        return result
    
    def _compact_session(self, session: Session) -> CompactResult:
        """
        Compact session memory by extracting key facts to Hot memory.
        
        Keeps recent 8 turns, archives older ones as key facts.
        """
        from markbot.session.manager import Session
        
        chat_id = session.key
        all_messages = session.messages
        
        if len(all_messages) <= self.DEFAULT_SESSION_WINDOW:
            return CompactResult([], len(all_messages), chat_id)
        
        # Messages to archive
        to_archive = all_messages[:-self.DEFAULT_SESSION_WINDOW]
        to_keep = all_messages[-self.DEFAULT_SESSION_WINDOW:]
        
        # Extract key facts (simple extraction for now)
        facts = []
        for msg in to_archive:
            role = msg.get("role", "")
            content = msg.get("content", "")
            
            if content and role in ["user", "assistant"]:
                # Extract sentences containing key information
                sentences = content.split('.')
                for sentence in sentences:
                    sentence = sentence.strip()
                    # Simple heuristic: longer sentences with specific keywords
                    if len(sentence) > 20 and any(kw in sentence.lower() for kw in 
                        ['decided', 'plan', 'todo', 'important', 'note', 'remember', 'key']):
                        facts.append(f"[{role}] {sentence}")
        
        # Add facts to Hot memory
        for fact in facts[:10]:  # Limit to 10 facts per compaction
            self.hot.add_important(fact, category="SessionCompact")
        
        # Update session
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
        
        Priority order:
        1. Whiteboard (L1 - current loop state)
        2. Session (L1.5 - recent turns)
        3. Hot (L2 - global important info)
        4. Warm (L3 - recent activity)
        5. Cold (L4 - semantic search results)
        """
        context = MemoryContext()
        
        # L1: Whiteboard (if loop is active)
        if include_whiteboard and chat_id in self._whiteboards:
            context.whiteboard_context = self._whiteboards[chat_id].get_context()
        
        # L1.5: Session history
        if session:
            history = session.get_history(max_messages=self.DEFAULT_SESSION_WINDOW)
            if history:
                lines = ["## Recent Conversation"]
                for msg in history:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")[:500]  # Truncate long messages
                    lines.append(f"**{role}**: {content}")
                context.session_context = "\n\n".join(lines)
        
        # L2: Hot memory
        context.hot_context = self.hot.get_context()
        
        # L3: Warm memory (recent activity)
        context.warm_context = self.warm.get_context(limit=3)
        
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
