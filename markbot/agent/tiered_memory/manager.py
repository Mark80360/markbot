"""MemoryManager - Central coordinator for the tiered memory system.

Orchestrates all memory layers and manages data flow between them.
Implements automated extraction, compaction, and lifecycle management.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import (
    MemoryTier, HotMemoryCategory,
    LoopState, ExecutionStep, SubtaskRecord
)
from .whiteboard import WhiteboardMemory
from .hot_memory import HotMemory
from .warm_memory import WarmMemory
from .cold_memory import ColdMemory, KnowledgeType
from .memory_sanitizer import MemorySanitizer


class MemoryManager:
    """
    Central coordinator for the complete tiered memory architecture.
    
    Architecture Overview:
    
        ┌─────────────────────────────────────────────┐
        │              L1 Whiteboard                  │
        │         (Loop-level workspace)              │
        └─────────────────┬───────────────────────────┘
                          │ (on loop completion)
                          ▼
        ┌─────────────────────────────────────────────┐
        │            L1.5 Session                     │
        │       (Sliding window context)              │
        └─────────────────┬───────────────────────────┘
                          │ (auto-extraction)
                          ▼
        ┌─────────────────────────────────────────────┐
        │              L2 Hot                         │
        │     (High-value facts, quality gate)        │
        └─────────────────┬───────────────────────────┘
                          │ (daily audit)
                          ▼
        ┌─────────────────────────────────────────────┐
        │               L3 Warm                       │
        │         (100% fidelity logs)                │
        └─────────────────┬───────────────────────────┘
                          │ (periodic archival)
                          ▼
        ┌─────────────────────────────────────────────┐
        │               L4 Cold                       │
        │      (Long-term semantic storage)           │
        └─────────────────────────────────────────────┘
    
    Key Responsibilities:
    - Initialize and manage all memory layers
    - Coordinate data flow between layers
    - Automated extraction from conversation turns
    - Context assembly for prompt injection
    - Lifecycle management (cleanup, compaction)
    """
    
    def __init__(
        self,
        workspace_path: str,
        enable_cold: bool = True,
        session_window: Optional[int] = None,
        hot_capacity: Optional[int] = None,
        warm_ttl_days: Optional[int] = None,
        compact_threshold: Optional[int] = None,
    ):
        """
        Initialize the memory manager with all layers.
        
        Args:
            workspace_path: Root path for all memory storage
            enable_cold: Whether to enable cold memory layer (default True)
            session_window: Number of turns to keep in session context (unused, kept for compatibility)
            hot_capacity: Maximum entries in hot memory (default 30)
            warm_ttl_days: TTL days for warm memory (default 30)
            compact_threshold: Threshold for session compaction (unused, kept for compatibility)
        """
        self.workspace_path = Path(workspace_path)
        
        # Initialize all memory layers
        self._sanitizer = MemorySanitizer()
        
        self.whiteboard = None      # Lazy init per session
        self.hot = HotMemory(
            str(self.workspace_path),
            max_entries=hot_capacity,
            sanitizer=self._sanitizer
        )
        self.warm = WarmMemory(str(self.workspace_path), ttl_days=warm_ttl_days)
        self.cold = ColdMemory(str(self.workspace_path)) if enable_cold else None
        
        # Configuration (stored for reference)
        self.enable_cold = enable_cold
        self.hot_capacity = hot_capacity
        self.warm_ttl_days = warm_ttl_days
        
        # Session tracking
        self._active_sessions: Dict[str, Dict[str, Any]] = {}
        
        # Statistics
        self._total_turns_processed = 0
        self._total_extractions = 0
        
        logger.info(
            f"[MemoryManager] Initialized at {workspace_path} "
            f"(cold={'enabled' if enable_cold else 'disabled'})"
        )
    
    def create_session(self, chat_id: str) -> WhiteboardMemory:
        """
        Create a new session with whiteboard.
        
        Each session gets its own L1 Whiteboard for task tracking.
        L1.5 Session context is managed implicitly.
        
        Args:
            chat_id: Unique session identifier
            
        Returns:
            Initialized WhiteboardMemory instance
        """
        if chat_id in self._active_sessions:
            logger.warning(f"[Manager] Session {chat_id} already exists")
            return self._active_sessions[chat_id]["whiteboard"]
        
        whiteboard = WhiteboardMemory(str(self.workspace_path), chat_id)
        
        self._active_sessions[chat_id] = {
            "whiteboard": whiteboard,
            "created_at": time.time(),
            "turn_count": 0,
            "last_activity": time.time(),
        }
        
        logger.info(f"[Manager] Created new session: {chat_id}")
        
        return whiteboard
    
    def get_session(self, chat_id: str) -> Optional[WhiteboardMemory]:
        """Get existing session's whiteboard."""
        session = self._active_sessions.get(chat_id)
        return session["whiteboard"] if session else None
    
    def close_session(self, chat_id: str) -> Dict[str, Any]:
        """
        Close a session and perform cleanup.
        
        Extracts valuable information to persistent layers before closing.
        
        Returns:
            Session summary statistics
        """
        session = self._active_sessions.get(chat_id)
        
        if not session:
            logger.warning(f"[Manager] Session {chat_id} not found")
            return {"status": "not_found"}
        
        whiteboard = session["whiteboard"]
        
        # Extract key information to hot memory
        summary = self._extract_from_whiteboard(whiteboard)
        
        # Remove from active sessions
        del self._active_sessions[chat_id]
        
        logger.info(
            f"[Manager] Closed session {chat_id}: "
            f"{session['turn_count']} turns processed"
        )
        
        return {
            "status": "closed",
            "turn_count": session["turn_count"],
            "duration_seconds": time.time() - session["created_at"],
            "extractions_made": len(summary.get("extracted", [])),
        }
    
    def process_turn(
        self,
        chat_id: str,
        user_input: str,
        assistant_response: str,
        turn_number: int = 0,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Process a complete dialogue turn through all memory layers.
        
        This is the MAIN ENTRY POINT for recording interactions.
        
        Data Flow:
        1. Update L1 Whiteboard (if exists)
        2. Log to L3 Warm Memory (100% fidelity)
        3. Auto-extract to L2 Hot Memory (quality gate)
        4. Optionally archive to L4 Cold Memory
        
        Args:
            chat_id: Session identifier
            user_input: User's message
            assistant_response: Assistant's response
            turn_number: Turn number in conversation
            metadata: Additional context
        """
        self._total_turns_processed += 1
        
        metadata = metadata or {}
        
        # Update session tracking
        if chat_id in self._active_sessions:
            self._active_sessions[chat_id]["turn_count"] += 1
            self._active_sessions[chat_id]["last_activity"] = time.time()
            
            # Update whiteboard
            whiteboard = self._active_sessions[chat_id]["whiteboard"]
            whiteboard.update_turn_context(turn_number, user_input, assistant_response)
        
        # Layer 3: Warm Memory (100% fidelity logging)
        self.warm.add_turn(
            chat_id=chat_id,
            user_input=user_input,
            assistant_response=assistant_response,
            turn_number=turn_number,
            metadata=metadata
        )
        
        # Auto-extraction to L2 Hot Memory
        extractions = self._auto_extract_from_turn(
            user_input=user_input,
            assistant_response=assistant_response,
            turn_number=turn_number,
            chat_id=chat_id
        )
        
        if extractions:
            self._total_extractions += len(extractions)
            logger.debug(
                f"[Manager] Auto-extracted {len(extractions)} items "
                f"from turn #{turn_number}"
            )
    
    def _auto_extract_from_turn(
        self,
        user_input: str,
        assistant_response: str,
        turn_number: int,
        chat_id: str
    ) -> List[str]:
        """
        Automatically extract high-value facts from a dialogue turn.
        
        Extraction Rules:
        - User preferences/intents → USER_INTENT category
        - Technical decisions → DECISION category
        - File operations mentioned → FILE_OP category
        - Error solutions → FIX category
        - Task completions → COMPLETION category
        
        Returns:
            List of extracted content strings that were accepted
        """
        extracted = []
        
        combined_text = f"{user_input}\n{assistant_response}"
        
        # Pattern-based extraction
        extraction_patterns = [
            {
                "category": HotMemoryCategory.USER_INTENT,
                "patterns": [
                    r'(?:I want|I need|please|can you|could you)\s+(.+?)(?:\.|$)',
                    r'(?:prefer|like|should be|must be)\s+(.+?)(?:\.|$)',
                ]
            },
            {
                "category": HotMemoryCategory.DECISION,
                "patterns": [
                    r"(?:decided|chose|selected|going to use)\s+(.+?)(?:\.|$)",
                    r"(?:we'll|we will|i'll|i will)\s+(?:use|implement|go with)\s+(.+?)(?:\.|$)",
                ]
            },
            {
                "category": HotMemoryCategory.FILE_OP,
                "patterns": [
                    r"(?:created|modified|deleted|updated)\s+(?:file\s+)?[`']?([^`'\n]+)[`']?",
                    r"(?:write|edit|change)\s+(?:to\s+)?(?:file\s+)?[`']?([^`'\n]+)[`']?",
                ]
            },
            {
                "category": HotMemoryCategory.FIX,
                "patterns": [
                    r"(?:fixed|resolved|solved)\s+(?:the\s+)?(?:issue|bug|error|problem)(?:\s+(?:with|in)\s+(.+?))?(?:\.|$)",
                    r"(?:solution|workaround|fix)(?:\s+is|:)\s*(.+?)(?:\.|$)",
                ]
            },
            {
                "category": HotMemoryCategory.COMPLETION,
                "patterns": [
                    r"(?:completed|finished|done)\s+(?:the\s+)?(?:task|job|work)(?:\s+(?:of|to)\s*(.+?))?(?:\.|$)",
                    r"(?:successfully)\s+(.+?)(?:\.|$)",
                ]
            }
        ]
        
        import re
        
        for extraction in extraction_patterns:
            category = extraction["category"]
            
            for pattern in extraction["patterns"]:
                matches = re.findall(pattern, combined_text, re.IGNORECASE)
                
                for match in matches:
                    if isinstance(match, tuple):
                        match = match[0] if match[0] else ""
                    
                    match = match.strip()
                    
                    if len(match) < 15:
                        continue
                    
                    # Add context to extracted fact
                    full_content = f"[Turn #{turn_number}] {match}"
                    
                    success = self.hot.add_important(
                        content=full_content,
                        category=category,
                        source_turn=turn_number,
                        source_session=chat_id,
                        confidence=0.7  # Auto-extracted has lower confidence
                    )
                    
                    if success:
                        extracted.append(full_content)
        
        return extracted
    
    def _extract_from_whiteboard(
        self, 
        whiteboard: WhiteboardMemory
    ) -> Dict[str, Any]:
        """
        Extract valuable information from completed whiteboard.
        
        Called when a session/loop is closed.
        Extracts decisions, errors encountered, and outcomes.
        
        Returns:
            Dictionary with extraction results
        """
        data = whiteboard.read_all()
        extracted = []
        
        # Extract task specification as DECISION
        task_spec = data.get("task_specification", "")
        if task_spec and len(task_spec) > 20:
            success = self.hot.add_important(
                content=f"[Session Complete] Task: {task_spec}",
                category=HotMemoryCategory.DECISION,
                confidence=0.9
            )
            if success:
                extracted.append(task_spec)
        
        # Extract error patterns
        errors = data.get("error_log", [])
        for error in errors[-3:]:  # Last 3 errors only
            error_str = str(error)
            if len(error_str) > 20:
                success = self.hot.add_important(
                    content=f"[Error Pattern] {error_str}",
                    category=HotMemoryCategory.ERROR_PATTERN,
                    confidence=0.8
                )
                if success:
                    extracted.append(error_str)
        
        # Extract completed subtasks
        completed = data.get("completed_subtasks", [])
        for subtask in completed[-5:]:  # Last 5 subtasks
            subtask_str = str(subtask)
            if len(subtask_str) > 20:
                success = self.hot.add_important(
                    content=f"[Completed] {subtask_str}",
                    category=HotMemoryCategory.COMPLETION,
                    confidence=0.85
                )
                if success:
                    extracted.append(subtask_str)
        
        return {
            "extracted": extracted,
            "count": len(extracted)
        }
    
    def get_full_context(
        self,
        chat_id: Optional[str] = None,
        query: Optional[str] = None,
        include_layers: Optional[List[MemoryTier]] = None
    ) -> str:
        """
        Assemble comprehensive memory context for prompt injection.
        
        This is used to inject memory into AI prompts.
        
        Layer Priority (for context assembly):
        1. L1 Whiteboard: Current task state (if session exists)
        2. L2 Hot: High-value recent facts
        3. L3 Warm: Recent activity log (limited)
        4. L4 Cold: Relevant long-term knowledge (if query provided)
        
        Args:
            chat_id: Current session ID (optional)
            query: Search query for semantic retrieval (optional)
            include_layers: Specific layers to include (default: all)
            
        Returns:
            Formatted markdown string with assembled context
        """
        if include_layers is None:
            include_layers = [MemoryTier.WHITEBOARD, MemoryTier.HOT, 
                           MemoryTier.WARM, MemoryTier.COLD]
        
        context_parts = []
        
        # Layer 1: Whiteboard (current task context)
        if MemoryTier.WHITEBOARD in include_layers and chat_id:
            whiteboard = self.get_session(chat_id)
            
            if whiteboard:
                wb_context = whiteboard.get_context()
                
                if wb_context:
                    context_parts.append(wb_context)
        
        # Layer 2: Hot Memory (high-value facts)
        if MemoryTier.HOT in include_layers:
            hot_context = self.hot.get_context(query=query, limit=10)
            
            if hot_context:
                context_parts.append(hot_context)
        
        # Layer 3: Warm Memory (recent activity, limited)
        if MemoryTier.WARM in include_layers:
            warm_context = self.warm.get_context(
                limit=5,
                chat_id=chat_id
            )
            
            if warm_context:
                context_parts.append(warm_context)
        
        # Layer 4: Cold Memory (semantic search results)
        if MemoryTier.COLD in include_layers and query and self.cold:
            cold_results = self.cold.semantic_search(
                query=query,
                limit=3,
                min_similarity=0.4
            )
            
            if cold_results:
                cold_context = ["## 📚 Long-term Knowledge (L4)"]
                
                for result in cold_results:
                    cold_context.append(
                        f"- **[{result['similarity']:.0%}]** "
                        f"{result['content'][:200]}..."
                    )
                
                context_parts.append("\n".join(cold_context))
        
        if not context_parts:
            return ""
        
        # Combine all contexts
        full_context = (
            "# 🧠 Memory Context\n\n"
            + "\n---\n".join(context_parts)
            + "\n---\n"
            f"*Generated at {time.strftime('%Y-%m-%d %H:%M:%S')}*"
        )
        
        return full_context
    
    def add_to_hot_memory(
        self,
        content: str,
        category: Union[str, HotMemoryCategory] = HotMemoryCategory.TURN_FACT,
        **metadata
    ) -> bool:
        """
        Convenience method to add directly to Hot Memory.
        
        All entries pass through quality gate automatically.
        
        Args:
            content: Content to store
            category: One of the 9 valid categories
            **metadata: Additional metadata (source_turn, etc.)
            
        Returns:
            True if entry was accepted
        """
        return self.hot.add_important(
            content=content,
            category=category,
            **metadata
        )
    
    def add_to_cold_memory(
        self,
        content: str,
        knowledge_type: Union[str, KnowledgeType] = KnowledgeType.FACT,
        tags: Optional[List[str]] = None,
        **metadata
    ) -> bool:
        """
        Convenience method to add directly to Cold Memory.
        
        For long-term knowledge storage with semantic search capability.
        
        Args:
            content: Structured knowledge content
            knowledge_type: One of the 7 knowledge types
            tags: Searchable keywords
            **metadata: Additional metadata
            
        Returns:
            True if entry was stored successfully
        """
        if not self.cold:
            logger.warning("[Manager] Cold memory is disabled, cannot add entry")
            return False
        return self.cold.add_entry(
            content=content,
            knowledge_type=knowledge_type,
            tags=tags,
            **metadata
        )
    
    def search_memory(
        self,
        query: str,
        search_hot: bool = True,
        search_cold: bool = True,
        limit: int = 10
    ) -> Dict[str, List[Any]]:
        """
        Search across multiple memory layers.
        
        Unified search interface for finding relevant information.
        
        Args:
            query: Search query string
            search_hot: Include L2 Hot Memory in search
            search_cold: Include L4 Cold Memory in search
            limit: Maximum results per layer
            
        Returns:
            Dictionary with results from each layer
        """
        results = {}
        
        if search_hot:
            hot_results = self.hot.search_entries(query, limit=limit)
            results["hot"] = hot_results
        
        if search_cold and self.cold:
            cold_results = self.cold.semantic_search(query, limit=limit)
            results["cold"] = cold_results
        
        return results
    
    def run_lifecycle_tasks(self) -> Dict[str, Any]:
        """
        Run periodic maintenance tasks.
        
        Should be called periodically (e.g., daily or hourly).
        
        Tasks:
        1. Clean up expired warm memory logs
        2. Archive old hot memory entries to cold
        3. Remove duplicate cold memory entries
        4. Generate statistics report
        
        Returns:
            Dictionary with maintenance results
        """
        results = {}
        
        # Task 1: Cleanup expired warm logs
        removed_logs = self.warm.cleanup_expired()
        results["warm_cleanup"] = {
            "removed_files": len(removed_logs),
            "files": removed_logs[:10]  # First 10 for reference
        }
        
        # Task 2: Duplicate cleanup in cold storage
        if self.cold:
            duplicates_removed = self.cold.cleanup_duplicates()
            results["cold_dedup"] = {
                "removed_count": duplicates_removed
            }
        else:
            results["cold_dedup"] = {"removed_count": 0, "skipped": "cold disabled"}
        
        # Task 3: Generate stats
        results["statistics"] = self.get_stats()
        
        logger.info("[Manager] Lifecycle tasks completed")
        
        return results
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get comprehensive statistics about all memory layers.
        
        Returns:
            Dictionary with layer-by-layer statistics
        """
        return {
            "manager": {
                "total_turns_processed": self._total_turns_processed,
                "total_extractions": self._total_extractions,
                "active_sessions": len(self._active_sessions),
                "workspace_path": str(self.workspace_path),
                "cold_enabled": self.enable_cold,
            },
            "hot_memory": self.hot.get_stats(),
            "warm_memory": self.warm.get_stats(),
            "cold_memory": self.cold.get_stats() if self.cold else {"status": "disabled"},
            "sanitizer": self._sanitizer.get_stats(),
        }
    
    def export_all_memory(self, output_dir: str) -> Dict[str, bool]:
        """
        Export all memory data for backup/migration.
        
        Exports each layer to separate files in the specified directory.
        
        Args:
            output_dir: Directory to export files to
            
        Returns:
            Dictionary indicating success/failure for each export
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        results = {}
        
        # Export Hot Memory
        try:
            hot_file = output_path / "hot_memory_export.md"
            hot_file.write_text(self.hot.read(), encoding="utf-8")
            results["hot_memory"] = True
        except Exception as e:
            logger.error(f"[Manager] Failed to export hot memory: {e}")
            results["hot_memory"] = False
        
        # Export Cold Memory
        if self.cold:
            results["cold_memory"] = self.cold.export_all(
                str(output_path / "cold_memory_export.json")
            )
        else:
            results["cold_memory"] = False
        
        # Export statistics
        try:
            stats_file = output_path / "memory_stats.json"
            stats_file.write_text(
                json.dumps(self.get_stats(), indent=2, default=str),
                encoding="utf-8"
            )
            results["statistics"] = True
        except Exception as e:
            logger.error(f"[Manager] Failed to export stats: {e}")
            results["statistics"] = False
        
        logger.info(
            f"[Manager] Exported memory to {output_dir}"
        )
        
        return results
    
    def clear_all_memory(self) -> None:
        """
        Clear ALL memory data across all layers.
        
        WARNING: This is irreversible! Use with extreme caution.
        Typically only used for testing or reset scenarios.
        """
        logger.warning("[Manager] ⚠️ CLEARING ALL MEMORY DATA!")
        
        # Clear all layers
        self.hot.clear()
        self.warm.clear()
        
        # Clear sessions
        self._active_sessions.clear()
        
        # Reset statistics
        self._total_turns_processed = 0
        self._total_extractions = 0
        
        logger.warning("[Manager] All memory data cleared")
