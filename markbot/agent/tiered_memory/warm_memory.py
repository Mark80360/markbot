"""L3 Warm Memory - Daily sequential logs."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

from .base import BaseMemoryLayer, MemoryTier


class WarmMemory(BaseMemoryLayer):
    """
    L3 Warm Memory: Sequential daily logs.
    
    - Stores full conversation logs per day
    - Files: workspace/memory/warm/YYYY-MM-DD.md
    - 30-day TTL (configurable)
    - Useful for reviewing recent activity
    
    Structure per file:
    ## [timestamp] Session: xxx
    **Role**: assistant/user
    
    content...
    """
    
    DEFAULT_TTL_DAYS = 30
    
    def __init__(self, workspace_path: str, ttl_days: int = None):
        super().__init__(MemoryTier.WARM, workspace_path)
        self.root = Path(workspace_path) / "memory" / "warm"
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_days = ttl_days or self.DEFAULT_TTL_DAYS
    
    def _get_today_file(self) -> Path:
        """Get today's log file path."""
        date_str = time.strftime("%Y-%m-%d")
        return self.root / f"{date_str}.md"
    
    def _get_file_for_date(self, date_str: str) -> Path:
        """Get log file path for specific date."""
        return self.root / f"{date_str}.md"
    
    def add_event(self, chat_id: str, role: str, content: str, 
                  metadata: Optional[Dict[str, Any]] = None) -> None:
        """Add an event to today's log."""
        file_path = self._get_today_file()
        timestamp = time.strftime("%H:%M:%S")
        
        # Build entry
        entry = f"\n## [{timestamp}] Session: {chat_id}\n"
        entry += f"**Role**: {role}\n\n"
        
        # Content with optional metadata
        if metadata:
            meta_str = ", ".join([f"{k}={v}" for k, v in metadata.items()])
            entry += f"<!-- {meta_str} -->\n"
        
        entry += f"{content}\n"
        
        # Append to file
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(entry)
    
    def add_turn(self, chat_id: str, user_input: str, assistant_response: str,
                 metadata: Optional[Dict[str, Any]] = None) -> None:
        """Add a full dialogue turn."""
        self.add_event(chat_id, "user", user_input, metadata)
        self.add_event(chat_id, "assistant", assistant_response, metadata)
    
    def read_today(self) -> str:
        """Read today's log."""
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
        """List recent log files."""
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
        """Remove log files older than TTL. Returns list of removed files."""
        removed = []
        cutoff = datetime.now() - timedelta(days=self.ttl_days)
        
        for file_path in self.root.glob("*.md"):
            try:
                date_str = file_path.stem
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    os.remove(file_path)
                    removed.append(file_path.name)
            except (ValueError, OSError):
                continue
        
        return removed
    
    def search_recent(self, query: str, days: int = 7, limit: int = 10) -> List[Dict[str, Any]]:
        """Search recent logs for query string."""
        results = []
        files = self.list_files(days=days)
        
        for file_path in files:
            content = file_path.read_text(encoding="utf-8")
            if query.lower() in content.lower():
                # Extract matching entries
                entries = content.split("\n## ")
                for entry in entries[1:]:  # Skip header
                    if query.lower() in entry.lower():
                        lines = entry.split('\n')
                        header = lines[0] if lines else ""
                        results.append({
                            "date": file_path.stem,
                            "header": header,
                            "preview": entry[:500],
                            "file": str(file_path)
                        })
                        
                        if len(results) >= limit:
                            return results
        
        return results
    
    # BaseMemoryLayer interface
    
    def add(self, content: str, **metadata) -> None:
        """Add content with chat_id from metadata."""
        chat_id = metadata.get("chat_id", "default")
        role = metadata.get("role", "assistant")
        self.add_event(chat_id, role, content, metadata)
    
    def get_context(self, query: Optional[str] = None, limit: int = 10) -> str:
        """Get recent warm memory context."""
        files = self.list_files(days=1)[:1]  # Just today
        if not files:
            return ""
        
        content = files[0].read_text(encoding="utf-8")
        lines = content.split('\n')
        
        # Get last N entries
        entries = []
        current_entry = []
        
        for line in reversed(lines):
            if line.startswith("## ["):
                if current_entry:
                    entries.append('\n'.join(reversed(current_entry)))
                    current_entry = []
                if len(entries) >= limit:
                    break
            current_entry.append(line)
        
        if current_entry:
            entries.append('\n'.join(reversed(current_entry)))
        
        if not entries:
            return ""
        
        context = ["## Recent Activity (Today)"]
        context.extend(reversed(entries))
        
        return '\n'.join(context)
    
    def clear(self) -> None:
        """Clear all warm memory files."""
        for file_path in self.root.glob("*.md"):
            os.remove(file_path)
    
    @property
    def is_persistent(self) -> bool:
        return True
