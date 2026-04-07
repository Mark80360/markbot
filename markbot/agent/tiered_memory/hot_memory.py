"""L2 Hot Memory - Short-term persistent memory with strict quality control.

Stores high-value extracted facts that persist across sessions.
All entries must pass through MemorySanitizer quality gate.
Implements FIFO capacity management with enhanced metadata tracking.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import (
    BaseMemoryLayer, MemoryTier, MemoryContext,
    HotMemoryCategory, HotMemoryEntry
)
from .memory_sanitizer import MemorySanitizer


class HotMemory(BaseMemoryLayer):
    """
    L2 Hot Memory: High-value short-term persistent memory.
    
    Characteristics:
    - Global/shared across all sessions (not per-chat)
    - Strict quality gate via MemorySanitizer (3-stage pipeline)
    - Max 30 entries with FIFO eviction policy
    - 9 categories of high-value content only
    
    Quality Gate (inspired by CoPaw's ReMeLight):
    1. Noise filter: reject internal monologue, conversational filler
    2. Secret redaction: auto-redact API keys, tokens, passwords
    3. Deduplication: reject near-duplicates (>85% similarity)
    
    Storage: workspace/memory/MEMORY.md with structured sections
    """
    
    DEFAULT_MAX_ENTRIES = 30
    MIN_CONTENT_LENGTH = 15     # Minimum content length to accept
    MAX_CONTENT_LENGTH = 500    # Maximum content length per entry
    
    def __init__(
        self, 
        workspace_path: str, 
        max_entries: Optional[int] = None,
        sanitizer: Optional[MemorySanitizer] = None
    ):
        super().__init__(MemoryTier.HOT, workspace_path)
        
        self.file_path = Path(workspace_path) / "memory" / "MEMORY.md"
        self.max_entries = max_entries or self.DEFAULT_MAX_ENTRIES
        self._sanitizer = sanitizer or MemorySanitizer()
        
        # In-memory entry cache for better management
        self._entries: List[HotMemoryEntry] = []
        
        # Statistics
        self._total_added = 0
        _total_rejected = 0
        
        self._ensure_file()
        self._load_entries()
        self._initialized = True
    
    def _ensure_file(self) -> None:
        """Ensure hot memory file exists with proper structure."""
        if not self.file_path.exists():
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            default_content = """# 🧠 Hot Memory (L2)

> **Purpose**: Store high-value facts extracted from conversations  
> **Capacity**: {max_entries} entries (FIFO eviction)  
> **Quality Gate**: All entries pass MemorySanitizer 3-stage check

## 📁 Past (Recent) - High-Value Facts

## 💡 Present - Active Work Context

## 🔮 Future - Planned Tasks

## ✅ Todo List

---
*Last updated: {timestamp}*
""".format(
                max_entries=self.max_entries,
                timestamp=time.strftime("%Y-%m-%d %H:%M")
            )
            self.file_path.write_text(default_content, encoding="utf-8")
            logger.info(f"[HotMemory] Created new MEMORY.md at {self.file_path}")
    
    def _load_entries(self) -> None:
        """Load existing entries from Markdown file into memory."""
        try:
            content = self.read()
            self._entries = self._parse_entries_from_markdown(content)
            logger.debug(
                f"[HotMemory] Loaded {len(self._entries)} existing entries"
            )
        except Exception as e:
            logger.warning(f"[HotMemory] Failed to load entries: {e}")
            self._entries = []
    
    def _parse_entries_from_markdown(self, content: str) -> List[HotMemoryEntry]:
        """Parse HotMemoryEntry objects from Markdown content."""
        entries = []
        in_past_section = False
        
        for line in content.split('\n'):
            if line.strip() == "## 📁 Past (Recent)" or line.strip() == "## Past (Recent)":
                in_past_section = True
                continue
            elif line.startswith("## ") and in_past_section:
                break
            
            if in_past_section and line.strip().startswith("- ["):
                try:
                    entry = self._parse_entry_line(line)
                    if entry:
                        entries.append(entry)
                except Exception as e:
                    logger.debug(f"[HotMemory] Failed to parse line: {e}")
        
        return entries
    
    def _parse_entry_line(self, line: str) -> Optional[HotMemoryEntry]:
        """Parse a single entry line into HotMemoryEntry."""
        # Format: - [2025-01-15 14:30] emoji `CATEGORY` content
        match = re.match(
            r'-\s*\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\]\s*'
            r'([^\s]+)\s+`([^`]+)`\s+(.+)',
            line.strip()
        )
        
        if match:
            timestamp_str, emoji, category_str, content = match.groups()
            
            try:
                category = HotMemoryCategory(category_str)
                created_at = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M")
                
                return HotMemoryEntry(
                    entry_id=str(uuid.uuid4())[:8],
                    content=content,
                    category=category,
                    created_at=created_at
                )
            except ValueError:
                logger.debug(f"[HotMemory] Unknown category: {category_str}")
                return None
        
        return None
    
    def read(self) -> str:
        """Read entire hot memory content."""
        self._ensure_file()
        return self.file_path.read_text(encoding="utf-8")
    
    def update(self, content: str) -> None:
        """Direct overwrite of hot memory file."""
        self.file_path.write_text(content, encoding="utf-8")
    
    def add_important(
        self, 
        content: str, 
        category: Union[str, HotMemoryCategory] = HotMemoryCategory.TURN_FACT,
        source_turn: int = 0,
        source_session: str = "",
        confidence: float = 0.8
    ) -> bool:
        """
        Add important information to Hot Memory.
        
        This is the PRIMARY method for writing to L2 Hot Memory.
        All entries MUST pass through the quality gate.
        
        Args:
            content: The fact/information to store
            category: One of the 9 valid categories
            source_turn: Source conversation turn number
            source_session: Source session ID
            confidence: Extraction confidence (0.0-1.0)
            
        Returns:
            True if entry was accepted and written
            False if rejected by quality gate
        """
        # Normalize category
        if isinstance(category, str):
            try:
                category = HotMemoryCategory(category.upper())
            except ValueError:
                logger.warning(f"[HotMemory] Invalid category '{category}', defaulting to TURN_FACT")
                category = HotMemoryCategory.TURN_FACT
        
        # Validate content length
        content = content.strip()
        if not content:
            return False
        
        if len(content) < self.MIN_CONTENT_LENGTH:
            logger.debug(
                f"[HotMemory] Rejected: too short ({len(content)} < {self.MIN_CONTENT_LENGTH})"
            )
            return False
        
        if len(content) > self.MAX_CONTENT_LENGTH:
            content = content[:self.MAX_CONTENT_LENGTH] + "..."
            logger.debug(f"[HotMemory] Content truncated to {self.MAX_CONTENT_LENGTH} chars")
        
        # Get existing entries for dedup checking
        existing_contents = [e.content for e in self._entries]
        
        # Apply quality gate (3-stage sanitizer)
        cleaned = self._sanitizer.clean_entry(content, existing_contents)
        if cleaned is None:
            logger.debug(
                f"[HotMemory] Entry rejected by quality gate: "
                f"{content[:80]}..."
            )
            return False
        
        content = cleaned
        
        # Create structured entry
        entry = HotMemoryEntry(
            entry_id=str(uuid.uuid4())[:8],
            content=content,
            category=category,
            confidence=min(max(confidence, 0.0), 1.0),
            source_turn=source_turn,
            source_session=source_session,
            tags=self._auto_generate_tags(content, category)
        )
        
        # Add to in-memory list
        self._entries.append(entry)
        
        # Enforce capacity limit (FIFO - remove oldest)
        if len(self._entries) > self.max_entries:
            removed = self._entries.pop(0)
            logger.debug(
                f"[HotMemory] Evicted oldest entry (FIFO): "
                f"{removed.entry_id[:8]}..."
            )
        
        # Persist to file
        self._persist_entries()
        
        self._total_added += 1
        logger.info(
            f"[HotMemory] ✓ Added [{category.value}] ({len(self._entries)}/{self.max_entries}): "
            f"{content[:60]}..."
        )
        
        return True
    
    def _auto_generate_tags(
        self, 
        content: str, 
        category: HotMemoryCategory
    ) -> List[str]:
        """Auto-generate tags based on content analysis."""
        tags = [category.value.lower()]
        
        # Detect common topics
        topic_keywords = {
            'python': ['python', 'pip', 'pypi', '.py'],
            'javascript': ['javascript', 'node', 'npm', '.js'],
            'database': ['sql', 'database', 'query', 'mysql', 'postgres'],
            'api': ['api', 'rest', 'endpoint', 'http'],
            'security': ['security', 'auth', 'token', 'password', 'key'],
            'performance': ['performance', 'optimize', 'speed', 'cache'],
            'error': ['error', 'exception', 'bug', 'fix', 'issue']
        }
        
        content_lower = content.lower()
        for topic, keywords in topic_keywords.items():
            if any(kw in content_lower for kw in keywords):
                tags.append(topic)
        
        return tags[:5]  # Limit to 5 tags max
    
    def add_todo(self, item: str) -> bool:
        """
        Add an item to Todo List section.
        
        Todos are simpler entries without full quality gate,
        but still undergo basic validation.
        """
        item = item.strip()
        if not item or len(item) < 5:
            return False
        
        content = self.read()
        lines = content.split('\n')
        new_lines = []
        inserted = False
        
        for line in lines:
            new_lines.append(line)
            if line.strip().startswith("##") and "Todo" in line and not inserted:
                new_lines.append(f"- [ ] {item}")
                inserted = True
        
        if not inserted:
            new_lines.append("\n## Todo List\n")
            new_lines.append(f"- [ ] {item}")
        
        self.update('\n'.join(new_lines))
        logger.info(f"[HotMemory] Added todo: {item[:50]}...")
        return True
    
    def complete_todo(self, item: str) -> bool:
        """Mark a todo item as completed."""
        content = self.read()
        if f"- [ ] {item}" in content:
            content = content.replace(f"- [ ] {item}", f"- [x] {item}")
            self.update(content)
            logger.info(f"[HotMemory] Completed todo: {item[:50]}...")
            return True
        return False
    
    def add_to_section(self, section: str, content: str) -> bool:
        """Add content to a specific section (Present/Future)."""
        current = self.read()
        lines = current.split('\n')
        new_lines = []
        inserted = False
        section_header = f"## {section}"
        
        for line in lines:
            new_lines.append(line)
            if line.strip().startswith(section_header) and not inserted:
                new_lines.append(content)
                inserted = True
        
        if not inserted:
            new_lines.append(f"\n{section_header}\n")
            new_lines.append(content)
        
        self.update('\n'.join(new_lines))
        return inserted
    
    def get_section(self, section: str) -> str:
        """
        Get content of a specific section.
        
        Supports fuzzy matching - will find section headers
        that contain the section name, even with emoji prefixes.
        """
        content = self.read()
        lines = content.split('\n')
        result = []
        in_section = False
        section_header = f"## {section}"
        
        for line in lines:
            line_stripped = line.strip()
            
            # Check if this line is the section header (with or without emoji)
            is_header = (
                line_stripped.startswith(section_header) or 
                (line_stripped.startswith("##") and section in line_stripped)
            )
            
            if is_header and not in_section:
                in_section = True
                continue
            elif line.startswith("## ") and in_section:
                break
            elif in_section:
                result.append(line)
        
        return "\n".join(result).strip()
    
    def _persist_entries(self) -> None:
        """Persist all in-memory entries to Markdown file."""
        lines = [
            "# 🧠 Hot Memory (L2)\n",
            f"> **Capacity**: {len(self._entries)}/{self.max_entries} entries | ",
            f"**Quality Gate**: Active | ",
            f"*Last updated: {time.strftime('%Y-%m-%d %H:%M')}*\n",
            "\n## 📁 Past (Recent) - High-Value Facts\n"
        ]
        
        # Write all entries in markdown format
        for entry in self._entries:
            lines.append(entry.to_markdown())
            lines.append("")
        
        # Preserve other sections (Todo, Present, Future)
        existing_content = self.read()
        other_sections = {}
        current_section = None
        
        for line in existing_content.split('\n'):
            if line.startswith("## "):
                current_section = line.strip()
                if current_section not in ["## 📁 Past (Recent)", "## Past (Recent)"]:
                    other_sections[current_section] = []
            elif current_section and current_section in other_sections:
                other_sections[current_section].append(line)
        
        # Append preserved sections
        for section_name, section_lines in other_sections.items():
            if any(line.strip() for line in section_lines):  # Non-empty section
                lines.append(f"\n{section_name}\n")
                lines.extend(section_lines)
        
        self.update('\n'.join(lines))
    
    def get_context(self, query: Optional[str] = None, limit: int = 10) -> str:
        """
        Get hot memory context for prompt injection.
        
        Returns formatted markdown with recent high-value facts and active todos.
        """
        if not self._entries:
            return ""
        
        context_lines = ["## 🔥 Hot Memory (L2 - Session-Agnostic Important Facts)"]
        
        # Get recent entries (respecting limit)
        recent_entries = self._entries[-limit:]
        
        for entry in recent_entries:
            context_lines.append(entry.to_markdown())
        
        # Add active todos
        todo_section = self.get_section("Todo List")
        if todo_section.strip():
            todos = [line for line in todo_section.split('\n') 
                     if line.strip().startswith("- [ ]")]
            if todos:
                context_lines.append("\n### ✅ Active Todos:")
                context_lines.extend(todos[:limit])
        
        return "\n".join(context_lines)
    
    def search_entries(
        self, 
        query: str, 
        category: Optional[HotMemoryCategory] = None,
        limit: int = 10
    ) -> List[HotMemoryEntry]:
        """
        Search entries by query text and optional category filter.
        
        Uses simple keyword matching (for advanced search, use Cold Memory).
        """
        results = []
        query_lower = query.lower()
        
        for entry in reversed(self._entries):  # Most recent first
            # Category filter
            if category and entry.category != category:
                continue
            
            # Keyword search in content and tags
            searchable = f"{entry.content} {' '.join(entry.tags)}".lower()
            if query_lower in searchable:
                results.append(entry)
                entry.access_count += 1
                
                if len(results) >= limit:
                    break
        
        return results
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about hot memory state."""
        category_counts = {}
        for entry in self._entries:
            cat_name = entry.category.value
            category_counts[cat_name] = category_counts.get(cat_name, 0) + 1
        
        return {
            "total_entries": len(self._entries),
            "max_capacity": self.max_entries,
            "utilization": len(self._entries) / self.max_entries,
            "total_added_ever": self._total_added,
            "category_breakdown": category_counts,
            "file_size_bytes": self.file_path.stat().st_size if self.file_path.exists() else 0
        }
    
    # BaseMemoryLayer interface implementation
    
    def add(self, content: str, **metadata) -> bool:
        """Add to Past section by default (with metadata support)."""
        category = metadata.get("category", HotMemoryCategory.TURN_FACT)
        source_turn = metadata.get("source_turn", 0)
        source_session = metadata.get("source_session", "")
        confidence = metadata.get("confidence", 0.8)
        
        return self.add_important(
            content=content,
            category=category,
            source_turn=source_turn,
            source_session=source_session,
            confidence=confidence
        )
    
    def clear(self) -> None:
        """Clear all hot memory data."""
        self._entries = []
        self._ensure_file()  # Reset to empty template
        logger.info("[HotMemory] Cleared all entries")
    
    @property
    def is_persistent(self) -> bool:
        """Hot memory persists to disk."""
        return True
    
    @property
    def entry_count(self) -> int:
        """Get current number of entries."""
        return len(self._entries)
