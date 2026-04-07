"""L3 Warm Memory - Daily sequential audit logs.

Stores complete conversation logs per day with 100% fidelity.
No quality filtering - this is the ground truth layer.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import BaseMemoryLayer, MemoryTier


class WarmMemory(BaseMemoryLayer):
    """
    L3 Warm Memory: Complete daily audit logs.
    
    Purpose:
    - 100% fidelity logging of all interactions (no filtering!)
    - Debugging and problem reproduction
    - Legal compliance and audit trail
    - Data source for L2 extraction and L4 archiving
    
    Characteristics:
    - Stores FULL conversation content (user + assistant)
    - Includes tool calls and results
    - Partitioned by date (one file per day)
    - 30-day TTL with automatic cleanup
    
    Storage: workspace/memory/warm/YYYY-MM-DD.md (append-only)
    """
    
    DEFAULT_TTL_DAYS = 30
    
    def __init__(self, workspace_path: str, ttl_days: Optional[int] = None):
        super().__init__(MemoryTier.WARM, workspace_path)
        self.root = Path(workspace_path) / "memory" / "warm"
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_days = ttl_days or self.DEFAULT_TTL_DAYS
        
        # Statistics
        self._total_events_today = 0
        self._initialized = True
    
    def _get_today_file(self) -> Path:
        """Get today's log file path."""
        date_str = time.strftime("%Y-%m-%d")
        return self.root / f"{date_str}.md"
    
    def _get_file_for_date(self, date_str: str) -> Path:
        """Get log file path for specific date."""
        return self.root / f"{date_str}.md"
    
    def add_event(
        self,
        chat_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        turn_number: Optional[int] = None
    ) -> None:
        """
        Add a single event to today's log.
        
        This is the core write operation for Warm Memory.
        No filtering or sanitization is applied.
        
        Args:
            chat_id: Session identifier
            role: 'user', 'assistant', 'system', or 'tool'
            content: Full message content (can be very long)
            metadata: Optional additional context
            turn_number: Conversation turn number
        """
        file_path = self._get_today_file()
        timestamp = time.strftime("%H:%M:%S")
        
        # Build structured entry
        entry_parts = [
            f"\n## [{timestamp}] Session: {chat_id}",
            f"**Role**: {role}"
        ]
        
        if turn_number is not None:
            entry_parts.append(f"**Turn**: #{turn_number}")
        
        if metadata:
            meta_str = ", ".join([f"{k}={v}" for k, v in metadata.items()])
            entry_parts.append(f"**Meta**: {meta_str}")
        
        entry_parts.append(f"\n{content}")
        
        entry = "\n".join(entry_parts)
        
        # Append to file (append-only, no overwriting)
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(entry)
            
            self._total_events_today += 1
            logger.debug(
                f"[Warm] Logged {role} event for {chat_id} "
                f"(today's total: {self._total_events_today})"
            )
        except Exception as e:
            logger.error(f"[Warm] Failed to write event: {e}")
    
    def add_turn(
        self,
        chat_id: str,
        user_input: str,
        assistant_response: str,
        turn_number: int = 0,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Add a complete dialogue turn (user + assistant).
        
        Args:
            chat_id: Session identifier
            user_input: User's message
            assistant_response: Assistant's response
            turn_number: Turn number in conversation
            metadata: Optional metadata
        """
        self.add_event(chat_id, "user", user_input, metadata, turn_number)
        self.add_event(
            chat_id, 
            "assistant", 
            assistant_response, 
            metadata, 
            turn_number
        )
    
    def add_system_event(
        self,
        event_type: str,
        description: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Add a system-level event (not tied to a specific session).
        
        Examples: startup, shutdown, config changes, errors
        """
        file_path = self._get_today_file()
        timestamp = time.strftime("%H:%M:%S")
        
        entry = (
            f"\n## [{timestamp}] System Event\n"
            f"**Type**: {event_type}\n"
            f"{description}\n"
        )
        
        if metadata:
            meta_str = "\n".join([f"- {k}: {v}" for k, v in metadata.items()])
            entry += f"\n**Details**:\n{meta_str}\n"
        
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as e:
            logger.error(f"[Warm] Failed to write system event: {e}")
    
    def read_today(self) -> str:
        """Read today's complete log."""
        file_path = self._get_today_file()
        if file_path.exists():
            return file_path.read_text(encoding="utf-8")
        return ""
    
    def read_date(self, date_str: str) -> str:
        """Read log for specific date (YYYY-MM-DD)."""
        file_path = self._get_file_for_date(date_str)
        if file_path.exists():
            return file_path.read_text(encoding="utf-8")
        return ""
    
    def list_files(self, days: int = 7) -> List[Path]:
        """List recent log files within specified days."""
        all_files = sorted(list(self.root.glob("*.md")), reverse=True)
        
        if days:
            cutoff = datetime.now() - timedelta(days=days)
            filtered = []
            for f in all_files:
                try:
                    date_str = f.stem
                    file_date = datetime.strptime(date_str, "%Y-%m-%d")
                    if file_date >= cutoff:
                        filtered.append(f)
                except ValueError:
                    continue
            return filtered
        
        return all_files
    
    def cleanup_expired(self) -> List[str]:
        """
        Remove log files older than TTL.
        
        Returns:
            List of removed filenames
        """
        removed = []
        cutoff = datetime.now() - timedelta(days=self.ttl_days)
        
        for file_path in self.root.glob("*.md"):
            try:
                date_str = file_path.stem
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                
                if file_date < cutoff:
                    os.remove(file_path)
                    removed.append(file_path.name)
                    logger.info(f"[Warm] Expired log removed: {file_path.name}")
                    
            except (ValueError, OSError) as e:
                logger.warning(f"[Warm] Error processing {file_path.name}: {e}")
        
        if removed:
            logger.info(f"[Warm] Cleaned up {len(removed)} expired log files")
        
        return removed
    
    def search_recent(
        self,
        query: str,
        days: int = 7,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Search recent logs for query string.
        
        Simple keyword search (for advanced semantic search, use Cold Memory).
        
        Returns:
            List of matching entries with metadata
        """
        results = []
        files = self.list_files(days=days)
        query_lower = query.lower()
        
        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8")
                
                if query_lower not in content.lower():
                    continue
                
                # Extract matching entries
                entries = content.split("\n## ")
                
                for entry in entries[1:]:  # Skip header
                    if query_lower in entry.lower():
                        lines = entry.split('\n')
                        header = lines[0] if lines else ""
                        
                        # Extract preview (first 500 chars of content)
                        preview_lines = [l for l in lines[2:] if l.strip()]
                        preview = '\n'.join(preview_lines)[:500]
                        
                        results.append({
                            "date": file_path.stem,
                            "header": header.strip(),
                            "preview": preview,
                            "file": str(file_path),
                            "match_strength": self._calculate_match_strength(query_lower, entry.lower())
                        })
                        
                        if len(results) >= limit:
                            return results
                            
            except Exception as e:
                logger.warning(f"[Warm] Search error in {file_path}: {e}")
        
        # Sort by match strength (descending)
        results.sort(key=lambda x: x["match_strength"], reverse=True)
        
        return results[:limit]
    
    def _calculate_match_strength(self, query: str, text: str) -> float:
        """
        Calculate simple match strength (0.0 - 1.0).
        
        Based on frequency and position of matches.
        """
        words = query.split()
        if not words:
            return 0.0
        
        total_matches = sum(text.count(word) for word in words)
        word_count = len(text.split())
        
        if word_count == 0:
            return 0.0
        
        # Normalize by text length and query length
        strength = (total_matches / len(words)) / (word_count / 100)
        
        return min(strength, 1.0)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about warm memory state."""
        total_files = len(list(self.root.glob("*.md")))
        total_size = sum(f.stat().st_size for f in self.root.glob("*.md"))
        
        today_content = self.read_today()
        today_entries = today_content.count("\n## ")
        
        return {
            "total_log_files": total_files,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "ttl_days": self.ttl_days,
            "today_events": self._total_events_today,
            "today_entries": today_entries,
            "oldest_file": self._get_oldest_file_date(),
            "newest_file": self._get_newest_file_date()
        }
    
    def _get_oldest_file_date(self) -> Optional[str]:
        """Get date string of oldest log file."""
        files = sorted(self.root.glob("*.md"))
        if files:
            return files[0].stem
        return None
    
    def _get_newest_file_date(self) -> Optional[str]:
        """Get date string of newest log file."""
        files = sorted(self.root.glob("*.md"), reverse=True)
        if files:
            return files[0].stem
        return None
    
    # BaseMemoryLayer interface implementation
    
    def add(self, content: str, **metadata) -> bool:
        """Add content with chat_id from metadata."""
        chat_id = metadata.get("chat_id", "system")
        role = metadata.get("role", "assistant")
        turn_number = metadata.get("turn_number")
        
        self.add_event(chat_id, role, content, metadata, turn_number)
        return True
    
    def get_context(
        self,
        query: Optional[str] = None,
        limit: int = 10,
        chat_id: Optional[str] = None
    ) -> str:
        """
        Get recent warm memory context for prompt injection.
        
        Filters by chat_id if provided, otherwise shows mixed recent activity.
        """
        files = self.list_files(days=3)[:3]
        
        if not files:
            return ""
        
        all_entries = []
        
        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8")
                entries = content.split("\n## ")
                
                for entry in entries[1:]:
                    if chat_id and f"Session: {chat_id}" not in entry:
                        continue
                    
                    all_entries.append("## " + entry)
                    
            except Exception as e:
                logger.warning(f"[Warm] Context read error: {e}")
        
        if not all_entries:
            return ""
        
        # Get most recent entries (respecting limit)
        recent = all_entries[-limit:]
        
        context = ["## 📝 Recent Activity Log (L3 - Last 3 Days)"]
        
        if chat_id:
            context[0] += f" [Session: {chat_id}]"
        
        context.extend(recent)
        
        return "\n".join(context)
    
    def clear(self) -> None:
        """Clear ALL warm memory files (use with caution!)."""
        count = 0
        for file_path in self.root.glob("*.md"):
            try:
                file_path.unlink()
                count += 1
            except Exception as e:
                logger.warning(f"[Warm] Error deleting {file_path}: {e}")
        
        logger.warning(f"[Warm] Cleared {count} log files")
        self._total_events_today = 0
    
    @property
    def is_persistent(self) -> bool:
        """Warm memory persists to disk."""
        return True
