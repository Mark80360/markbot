"""L2 Hot Memory - Short-term persistent memory with capacity limit."""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

from .base import BaseMemoryLayer, MemoryTier


class HotMemory(BaseMemoryLayer):
    """
    L2 Hot Memory: Short-term persistent memory (shared across sessions).
    
    - Global/shared across all sessions (not per-chat)
    - Max 20 entries (configurable), oldest removed when exceeded
    - Categories: Past, Present, Future, Todo
    - Stores: Key facts, important info, TODOs, future plans
    
    File: workspace/memory/MEMORY.md
    """
    
    MAX_ENTRIES = 20
    
    def __init__(self, workspace_path: str, max_entries: int = None):
        super().__init__(MemoryTier.HOT, workspace_path)
        self.file_path = Path(workspace_path) / "memory" / "MEMORY.md"
        self.max_entries = max_entries or self.MAX_ENTRIES
        self._ensure_file()
    
    def _ensure_file(self) -> None:
        """Ensure hot memory file exists with default structure."""
        if not self.file_path.exists():
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            default_content = """# Hot Memory

## Past (Recent)

## Present

## Future

## Todo List

"""
            self.file_path.write_text(default_content, encoding="utf-8")
    
    def read(self) -> str:
        """Read entire hot memory content."""
        self._ensure_file()
        return self.file_path.read_text(encoding="utf-8")
    
    def update(self, content: str) -> None:
        """Direct overwrite of hot memory file."""
        self.file_path.write_text(content, encoding="utf-8")
    
    def add_important(self, content: str, category: str = "Important") -> None:
        """
        Add important information to Past section.
        Enforces capacity limit by removing oldest entries.
        """
        content = content.strip()
        if not content:
            return
        
        current = self.read()
        lines = current.split('\n')
        
        # Find Past section
        past_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == "## Past (Recent)":
                past_idx = i
                break
        
        if past_idx == -1:
            # Add Past section at top
            lines.insert(0, "## Past (Recent)")
            lines.insert(1, "")
            past_idx = 0
        
        # Add new entry with timestamp
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        new_entry = f"- [{timestamp}] {category}: {content}"
        
        # Find where Past section ends
        past_end = len(lines)
        for i in range(past_idx + 1, len(lines)):
            if lines[i].startswith("## "):
                past_end = i
                break
        
        # Insert new entry
        lines.insert(past_end, new_entry)
        
        # Enforce capacity limit
        lines = self._enforce_capacity(lines, past_idx)
        
        self.update('\n'.join(lines))
    
    def _enforce_capacity(self, lines: List[str], past_idx: int) -> List[str]:
        """Remove oldest entries if exceeding max_entries."""
        entry_lines = []
        other_lines = []
        
        in_past = False
        for i, line in enumerate(lines):
            if line.strip() == "## Past (Recent)":
                in_past = True
                other_lines.append(line)
            elif line.startswith("## ") and in_past:
                in_past = False
                other_lines.append(line)
            elif in_past and line.strip().startswith("- ["):
                entry_lines.append(line)
            elif in_past and line.strip() == "":
                continue
            else:
                other_lines.append(line)
        
        # Keep only newest entries
        entry_lines = entry_lines[-self.max_entries:]
        
        # Rebuild
        result = []
        in_past = False
        past_added = False
        
        for line in lines:
            if line.strip() == "## Past (Recent)":
                in_past = True
                past_added = True
                result.append(line)
            elif line.startswith("## ") and in_past:
                result.extend(entry_lines)
                result.append("")
                in_past = False
                result.append(line)
            elif in_past:
                if line.strip().startswith("- ["):
                    continue
                elif line.strip() == "":
                    continue
                else:
                    result.append(line)
            else:
                result.append(line)
        
        if past_added and in_past:
            result.extend(entry_lines)
        
        return result
    
    def append_todo(self, item: str) -> None:
        """Add an item to Todo List section."""
        content = self.read()
        lines = content.split('\n')
        new_lines = []
        inserted = False
        
        for line in lines:
            new_lines.append(line)
            if line.strip() == "## Todo List":
                new_lines.append(f"- [ ] {item}")
                inserted = True
        
        if not inserted:
            new_lines.append("\n## Todo List")
            new_lines.append(f"- [ ] {item}")
        
        self.update("\n".join(new_lines))
    
    def complete_todo(self, item: str) -> bool:
        """Mark a todo item as completed. Returns True if found."""
        content = self.read()
        if f"- [ ] {item}" in content:
            content = content.replace(f"- [ ] {item}", f"- [x] {item}")
            self.update(content)
            return True
        return False
    
    def add_to_section(self, section: str, content: str) -> None:
        """Add content to a specific section."""
        current = self.read()
        lines = current.split('\n')
        new_lines = []
        inserted = False
        section_header = f"## {section}"
        
        for line in lines:
            new_lines.append(line)
            if line.strip() == section_header:
                new_lines.append(content)
                inserted = True
        
        if not inserted:
            new_lines.append(f"\n{section_header}")
            new_lines.append(content)
        
        self.update("\n".join(new_lines))
    
    def get_section(self, section: str) -> str:
        """Get content of a specific section."""
        content = self.read()
        lines = content.split('\n')
        result = []
        in_section = False
        section_header = f"## {section}"
        
        for line in lines:
            if line.strip() == section_header:
                in_section = True
                continue
            elif line.startswith("## ") and in_section:
                break
            elif in_section:
                result.append(line)
        
        return "\n".join(result).strip()
    
    # BaseMemoryLayer interface
    
    def add(self, content: str, **metadata) -> None:
        """Add to Past section by default."""
        category = metadata.get("category", "General")
        self.add_important(content, category)
    
    def get_context(self, query: Optional[str] = None, limit: int = 10) -> str:
        """Get hot memory context for prompt injection."""
        content = self.read()
        lines = content.split('\n')
        context_lines = ["## Hot Memory (Session-Agnostic Important Info)"]
        
        # Get all entries from Past section
        in_past = False
        entries = []
        for line in lines:
            if line.strip() == "## Past (Recent)":
                in_past = True
                continue
            elif line.startswith("## ") and in_past:
                break
            elif in_past and line.strip().startswith("- ["):
                entries.append(line)
        
        # Add recent entries
        for entry in entries[-limit:]:
            context_lines.append(entry)
        
        # Add Todo items
        todo_section = self.get_section("Todo List")
        if todo_section.strip():
            context_lines.append("\n### Active Todos")
            for line in todo_section.split('\n')[:limit]:
                if line.strip().startswith("- [ ]"):
                    context_lines.append(line)
        
        return "\n".join(context_lines)
    
    def clear(self) -> None:
        """Clear hot memory (rarely used)."""
        self._ensure_file()
    
    @property
    def is_persistent(self) -> bool:
        return True
