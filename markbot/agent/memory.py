"""Enhanced memory system with categories, decay, layers, and entity tracking.

Architecture:
- Singleton pattern for thread-safe access
- Lazy persistence with dirty flag
- Atomic file writes
- Entity referential integrity
- Automatic history rotation
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from json_repair import repair_json
from loguru import logger

from markbot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from markbot.providers.base import LLMProvider
    from markbot.session.manager import Session


class MemoryCategory(Enum):
    """Memory category for classification."""
    IDENTITY = "identity"
    PREFERENCE = "preference"
    FACT = "fact"
    PROJECT = "project"
    TASK = "task"
    EVENT = "event"
    LESSON = "lesson"
    CONTACT = "contact"


class MemoryLayer(Enum):
    """Memory layers for selective loading."""
    CORE = "core"
    WORKING = "working"
    EPISODIC = "episodic"


# Configuration constants
MEMORY_CONFIG = {
    "max_entries": 500,
    "max_entities": 200,
    "min_relevance_threshold": 0.1,
    "entity_inactivity_days": 90,
    "max_file_size_mb": 5,
    "max_history_size_mb": 10,
    "core_token_budget": 500,
    "working_token_budget": 1500,
    "save_debounce_seconds": 3.0,
}

# Decay rates per category
DECAY_RATES: dict[MemoryCategory, float] = {
    MemoryCategory.IDENTITY: 0.001,
    MemoryCategory.PREFERENCE: 0.01,
    MemoryCategory.FACT: 0.02,
    MemoryCategory.PROJECT: 0.05,
    MemoryCategory.TASK: 0.1,
    MemoryCategory.EVENT: 0.03,
    MemoryCategory.LESSON: 0.015,
    MemoryCategory.CONTACT: 0.02,
}

# Default layer assignment per category
DEFAULT_LAYERS: dict[MemoryCategory, MemoryLayer] = {
    MemoryCategory.IDENTITY: MemoryLayer.CORE,
    MemoryCategory.PREFERENCE: MemoryLayer.CORE,
    MemoryCategory.FACT: MemoryLayer.WORKING,
    MemoryCategory.PROJECT: MemoryLayer.WORKING,
    MemoryCategory.TASK: MemoryLayer.WORKING,
    MemoryCategory.EVENT: MemoryLayer.EPISODIC,
    MemoryCategory.LESSON: MemoryLayer.EPISODIC,
    MemoryCategory.CONTACT: MemoryLayer.WORKING,
}

# Importance defaults per category
DEFAULT_IMPORTANCE: dict[MemoryCategory, float] = {
    MemoryCategory.IDENTITY: 0.95,
    MemoryCategory.PREFERENCE: 0.7,
    MemoryCategory.FACT: 0.6,
    MemoryCategory.PROJECT: 0.7,
    MemoryCategory.TASK: 0.5,
    MemoryCategory.EVENT: 0.4,
    MemoryCategory.LESSON: 0.65,
    MemoryCategory.CONTACT: 0.6,
}


@dataclass
class Entity:
    """Tracked entity (person, project, topic)."""
    name: str
    entity_type: str
    aliases: list[str] = field(default_factory=list)
    first_mentioned: datetime = field(default_factory=datetime.now)
    last_mentioned: datetime = field(default_factory=datetime.now)
    mention_count: int = 1
    related_entities: list[str] = field(default_factory=list)
    context: str = ""

    def update_mention(self, context: str = "") -> None:
        """Update on new mention."""
        self.last_mentioned = datetime.now()
        self.mention_count += 1
        if context and context != self.context:
            self.context = self.context or context

    def compute_relevance(self, now: datetime | None = None) -> float:
        """Compute relevance score based on recency and frequency."""
        now = now or datetime.now()
        age_days = (now - self.last_mentioned).days
        time_decay = math.exp(-0.02 * max(0, age_days))
        frequency_boost = min(1.5, 1.0 + self.mention_count * 0.1)
        return time_decay * frequency_boost

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "name": self.name,
            "entity_type": self.entity_type,
            "aliases": self.aliases,
            "first_mentioned": self.first_mentioned.isoformat(),
            "last_mentioned": self.last_mentioned.isoformat(),
            "mention_count": self.mention_count,
            "related_entities": self.related_entities,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entity:
        """Deserialize from dict."""
        return cls(
            name=data["name"],
            entity_type=data["entity_type"],
            aliases=data.get("aliases", []),
            first_mentioned=datetime.fromisoformat(data["first_mentioned"]) if data.get("first_mentioned") else datetime.now(),
            last_mentioned=datetime.fromisoformat(data["last_mentioned"]) if data.get("last_mentioned") else datetime.now(),
            mention_count=data.get("mention_count", 1),
            related_entities=data.get("related_entities", []),
            context=data.get("context", ""),
        )


@dataclass
class MemoryEntry:
    """A single memory entry with metadata."""
    id: str
    content: str
    category: MemoryCategory
    layer: MemoryLayer
    importance: float = 0.5
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)
    access_count: int = 0
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    source_session: str | None = None
    source_timestamp: str | None = None

    @property
    def decay_rate(self) -> float:
        return DECAY_RATES.get(self.category, 0.02)

    def compute_relevance(self, now: datetime | None = None) -> float:
        """Compute current relevance score with time decay."""
        now = now or datetime.now()
        age_days = (now - self.last_accessed).days
        time_decay = math.exp(-self.decay_rate * max(0, age_days))
        frequency_boost = min(1.0, self.access_count * 0.1)
        return self.importance * time_decay * (1 + frequency_boost)

    def access(self) -> None:
        """Mark this entry as accessed."""
        self.last_accessed = datetime.now()
        self.access_count += 1

    def estimate_tokens(self) -> int:
        """Estimate token count for this entry."""
        return max(1, len(self.content) // 4)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category.value,
            "layer": self.layer.value,
            "importance": self.importance,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "access_count": self.access_count,
            "tags": self.tags,
            "entities": self.entities,
            "source_session": self.source_session,
            "source_timestamp": self.source_timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        """Deserialize from dict."""
        category = MemoryCategory(data["category"])
        return cls(
            id=data["id"],
            content=data["content"],
            category=category,
            layer=MemoryLayer(data.get("layer", DEFAULT_LAYERS.get(category, MemoryLayer.WORKING).value)),
            importance=max(0.0, min(1.0, data.get("importance", 0.5))),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
            last_accessed=datetime.fromisoformat(data["last_accessed"]) if data.get("last_accessed") else datetime.now(),
            access_count=max(0, data.get("access_count", 0)),
            tags=data.get("tags", []),
            entities=data.get("entities", []),
            source_session=data.get("source_session"),
            source_timestamp=data.get("source_timestamp"),
        )

    def to_markdown_line(self) -> str:
        """Format as markdown list item."""
        marker = "★ " if self.importance >= 0.8 else ""
        return f"- {marker}{self.content}"


class EntityTracker:
    """
    Track entities mentioned in conversations.
    
    Entities are extracted by AI during consolidation, not by regex patterns.
    This avoids the limitations of pattern matching and leverages AI's understanding.
    """

    def __init__(self):
        self.entities: dict[str, Entity] = {}

    def add_or_update(self, name: str, entity_type: str = "topic", context: str = "") -> None:
        """Add a new entity or update existing one."""
        existing_key = next((k for k in self.entities if k.lower() == name.lower()), None)
        if existing_key:
            self.entities[existing_key].update_mention(context)
        else:
            self.entities[name] = Entity(name=name, entity_type=entity_type, context=context)

    def get_recent_entities(self, days: int = 7, limit: int = 10) -> list[Entity]:
        """Get entities mentioned in the last N days."""
        cutoff = datetime.now() - timedelta(days=days)
        recent = [e for e in self.entities.values() if e.last_mentioned >= cutoff]
        recent.sort(key=lambda e: (e.mention_count, e.last_mentioned), reverse=True)
        return recent[:limit]

    def to_dict(self) -> dict[str, Any]:
        return {name: entity.to_dict() for name, entity in self.entities.items()}

    def from_dict(self, data: dict[str, Any]) -> None:
        self.entities = {name: Entity.from_dict(e) for name, e in data.items()}

    def prune_inactive(self, days: int, max_entities: int) -> int:
        """Remove inactive entities. Returns count removed."""
        initial_count = len(self.entities)
        cutoff = datetime.now() - timedelta(days=days)

        # Keep active entities
        active = {name: entity for name, entity in self.entities.items() if entity.last_mentioned >= cutoff}

        # If over limit, keep top by relevance
        if len(active) > max_entities:
            scored = sorted(active.items(), key=lambda x: x[1].compute_relevance(), reverse=True)
            active = dict(scored[:max_entities])

        self.entities = active
        return initial_count - len(self.entities)


# Tool schema for memory consolidation
_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save consolidated memories to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entries": {
                        "type": "array",
                        "description": "List of memory entries to save.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "The memory content."},
                                "category": {
                                    "type": "string",
                                    "enum": ["identity", "preference", "fact", "project", "task", "event", "lesson", "contact"],
                                },
                                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "entities": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["content", "category"],
                        },
                    },
                    "history_entry": {"type": "string"},
                },
                "required": ["entries"],
            },
        },
    }
]


class MemoryStore:
    """
    Enhanced memory system with singleton pattern.

    Features:
    - Singleton for thread-safe access
    - Lazy persistence with dirty flag
    - Atomic file writes
    - Entity referential integrity
    - Automatic history rotation
    - Auto-save on garbage collection
    """

    # Singleton instances per workspace
    _instances: ClassVar[dict[str, MemoryStore]] = {}
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __new__(cls, workspace: Path) -> MemoryStore:
        key = str(workspace.resolve())

        with cls._lock:
            if key not in cls._instances:
                instance = super().__new__(cls)
                cls._instances[key] = instance
                instance._initialized = False
            return cls._instances[key]

    def __init__(self, workspace: Path):
        # Skip if already initialized (singleton)
        if getattr(self, '_initialized', False):
            return

        self.memory_dir = ensure_dir(workspace / "memory")
        self.entries_file = self.memory_dir / "ENTRIES.json"
        self.entities_file = self.memory_dir / "ENTITIES.json"
        self.history_file = self.memory_dir / "HISTORY.md"
        self.memory_md_file = self.memory_dir / "MEMORY.md"

        self.entries: list[MemoryEntry] = []
        self.entity_tracker = EntityTracker()
        self._next_id = 1
        self._dirty = False
        self._save_task: asyncio.Task | None = None

        self._load()
        self._initialized = True

    def __del__(self) -> None:
        """Destructor: save dirty data on garbage collection."""
        if getattr(self, '_dirty', False):
            try:
                self._save_now()
                logger.debug("MemoryStore: auto-saved on __del__")
            except Exception as e:
                logger.warning("MemoryStore: failed to auto-save on __del__: {}", e)

    @classmethod
    def clear_instance(cls, workspace: Path) -> None:
        """Clear singleton instance (for testing)."""
        key = str(workspace.resolve())
        cls._instances.pop(key, None)

    def _load(self) -> None:
        """Load entries and entities from disk."""
        # Load entries
        if self.entries_file.exists():
            try:
                data = json.loads(self.entries_file.read_text(encoding="utf-8"))
                self.entries = [MemoryEntry.from_dict(e) for e in data.get("entries", [])]
                self._next_id = data.get("next_id", len(self.entries) + 1)
            except Exception as e:
                logger.warning("Failed to load memory entries: {}", e)

        # Load entities
        if self.entities_file.exists():
            try:
                data = json.loads(self.entities_file.read_text(encoding="utf-8"))
                self.entity_tracker.from_dict(data)
            except Exception as e:
                logger.warning("Failed to load entities: {}", e)

        # Migrate from MEMORY.md if no entries exist
        if not self.entries and self.memory_md_file.exists():
            self._migrate_from_memory_md()

    def _migrate_from_memory_md(self) -> None:
        """One-time migration from MEMORY.md."""
        try:
            content = self.memory_md_file.read_text(encoding="utf-8")
            current_category = MemoryCategory.FACT

            for line in content.split("\n"):
                if line.startswith("## "):
                    section = line[3:].lower()
                    if "identity" in section:
                        current_category = MemoryCategory.IDENTITY
                    elif "preference" in section:
                        current_category = MemoryCategory.PREFERENCE
                    elif "project" in section:
                        current_category = MemoryCategory.PROJECT
                    else:
                        current_category = MemoryCategory.FACT
                elif line.startswith("- "):
                    item = line[2:].strip()
                    if item.startswith("★ "):
                        item = item[2:]
                    if item and not item.startswith("["):
                        entry = self._create_entry(content=item, category=current_category)
                        self.entries.append(entry)

            logger.info("Migrated {} entries from MEMORY.md", len(self.entries))
            self._save_now()
        except Exception as e:
            logger.warning("Failed to migrate from MEMORY.md: {}", e)

    def _atomic_write(self, file_path: Path, content: str) -> None:
        """Write to file atomically using temp file."""
        temp_path = file_path.with_suffix('.tmp')
        try:
            temp_path.write_text(content, encoding="utf-8")
            temp_path.replace(file_path)
        except Exception as e:
            # Clean up temp file on failure
            if temp_path.exists():
                temp_path.unlink()
            raise e

    def _save_now(self) -> None:
        """Save immediately (blocking)."""
        # Save entries
        data = {
            "entries": [e.to_dict() for e in self.entries],
            "next_id": self._next_id,
        }
        self._atomic_write(self.entries_file, json.dumps(data, ensure_ascii=False, indent=2))

        # Save entities
        self._atomic_write(self.entities_file, json.dumps(self.entity_tracker.to_dict(), ensure_ascii=False, indent=2))

        # Sync MEMORY.md
        self._sync_to_memory_md()

        self._dirty = False

    def mark_dirty(self) -> None:
        """Mark memory as needing save. Triggers debounced save."""
        self._dirty = True

        # Cancel existing save task
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()

        # Schedule new save
        async def _delayed_save():
            try:
                await asyncio.sleep(MEMORY_CONFIG["save_debounce_seconds"])
                if self._dirty:
                    self._save_now()
            except asyncio.CancelledError:
                pass

        try:
            loop = asyncio.get_running_loop()
            self._save_task = loop.create_task(_delayed_save())
        except RuntimeError:
            # No event loop, save immediately
            self._save_now()

    def _sync_to_memory_md(self) -> None:
        """Sync entries to MEMORY.md for human readability."""
        by_category: dict[MemoryCategory, list[MemoryEntry]] = {}
        for entry in self.entries:
            by_category.setdefault(entry.category, []).append(entry)

        category_order = [
            (MemoryCategory.IDENTITY, "Identity"),
            (MemoryCategory.PREFERENCE, "Preferences"),
            (MemoryCategory.FACT, "Important Facts"),
            (MemoryCategory.PROJECT, "Project Context"),
            (MemoryCategory.TASK, "Tasks"),
            (MemoryCategory.EVENT, "Events"),
            (MemoryCategory.LESSON, "Lessons Learned"),
            (MemoryCategory.CONTACT, "Contacts"),
        ]

        lines = [
            "# Long-term Memory",
            "",
            "This file stores important information that persists across sessions.",
            "*Auto-generated from ENTRIES.json. Edits will be overwritten.*",
            "",
        ]

        for category, display_name in category_order:
            entries = by_category.get(category, [])
            if not entries:
                continue
            entries.sort(key=lambda e: (-e.importance, e.layer != MemoryLayer.CORE))
            lines.append(f"## {display_name}")
            lines.append("")
            for entry in entries:
                lines.append(entry.to_markdown_line())
            lines.append("")

        # Entities section
        if self.entity_tracker.entities:
            lines.extend(["## Entities", ""])
            for entity in sorted(self.entity_tracker.entities.values(), key=lambda e: -e.mention_count):
                lines.append(f"- {entity.name} ({entity.entity_type}): {entity.context or 'no context'}")
            lines.append("")

        lines.extend([
            "---",
            f"*Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        ])

        self._atomic_write(self.memory_md_file, "\n".join(lines))

    def _generate_id(self) -> str:
        """Generate unique entry ID."""
        entry_id = f"mem_{self._next_id}"
        self._next_id += 1
        return entry_id

    def _create_entry(
        self,
        content: str,
        category: MemoryCategory,
        importance: float | None = None,
        tags: list[str] | None = None,
        entities: list[str] | None = None,
        layer: MemoryLayer | None = None,
    ) -> MemoryEntry:
        """Create a new memory entry."""
        if importance is None:
            importance = DEFAULT_IMPORTANCE.get(category, 0.5)
        importance = max(0.0, min(1.0, importance))

        if layer is None:
            layer = DEFAULT_LAYERS.get(category, MemoryLayer.WORKING)

        return MemoryEntry(
            id=self._generate_id(),
            content=content,
            category=category,
            layer=layer,
            importance=importance,
            tags=tags or [],
            entities=entities or [],
        )

    def add_entry(
        self,
        content: str,
        category: MemoryCategory,
        importance: float | None = None,
        tags: list[str] | None = None,
        entities: list[str] | None = None,
    ) -> MemoryEntry:
        """Add a new memory entry."""
        if not content or not content.strip():
            raise ValueError("Memory content cannot be empty")

        entry = self._create_entry(content.strip(), category, importance, tags, entities)
        self.entries.append(entry)

        # Update entity tracker (entities are typically extracted by AI during consolidation)
        for entity_name in entry.entities:
            self.entity_tracker.add_or_update(entity_name, entity_type="topic", context=content[:100])

        self.mark_dirty()
        logger.debug("Added memory entry: {} [{}]", content[:50], category.value)
        return entry

    def get_entries_by_layer(self, layer: MemoryLayer) -> list[MemoryEntry]:
        return [e for e in self.entries if e.layer == layer]

    def get_entries_by_category(self, category: MemoryCategory) -> list[MemoryEntry]:
        return [e for e in self.entries if e.category == category]

    def search_entries(
        self,
        query: str,
        categories: list[MemoryCategory] | None = None,
        layers: list[MemoryLayer] | None = None,
        tags: list[str] | None = None,
        min_importance: float = 0.0,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Search entries by query and filters."""
        query_lower = query.lower()
        scored: list[tuple[MemoryEntry, float]] = []

        for entry in self.entries:
            if categories and entry.category not in categories:
                continue
            if layers and entry.layer not in layers:
                continue
            if tags and not any(t in entry.tags for t in tags):
                continue
            if entry.importance < min_importance:
                continue

            content_lower = entry.content.lower()
            if query_lower in content_lower:
                match_score = 1.0
            elif any(t.lower() in query_lower for t in entry.tags):
                match_score = 0.8
            elif any(e.lower() in query_lower for e in entry.entities):
                match_score = 0.7
            else:
                continue

            relevance = entry.compute_relevance() * match_score
            scored.append((entry, relevance))

        scored.sort(key=lambda x: x[1], reverse=True)
        result = [e for e, _ in scored[:limit]]

        # Mark as accessed and trigger save
        for entry in result:
            entry.access()
        if result:
            self.mark_dirty()

        return result

    def get_memory_context(
        self,
        current_message: str = "",
        max_tokens: int = 2000,
    ) -> str:
        """Build memory context for LLM prompt."""
        parts: list[str] = []
        token_count = 0

        # Core layer
        core_entries = sorted(
            self.get_entries_by_layer(MemoryLayer.CORE),
            key=lambda e: e.importance,
            reverse=True,
        )
        core_lines = []
        for entry in core_entries:
            if token_count + entry.estimate_tokens() <= MEMORY_CONFIG["core_token_budget"]:
                core_lines.append(entry.to_markdown_line())
                token_count += entry.estimate_tokens()
                entry.access()

        if core_lines:
            parts.append("## Core Memory\n" + "\n".join(core_lines))

        # Working layer
        working_entries = self.get_entries_by_layer(MemoryLayer.WORKING)
        if working_entries:
            scored: list[tuple[MemoryEntry, float]] = []
            query_lower = current_message.lower()

            for entry in working_entries:
                base_relevance = entry.compute_relevance()
                if query_lower:
                    if query_lower in entry.content.lower():
                        base_relevance *= 1.5
                    elif any(t.lower() in query_lower for t in entry.tags):
                        base_relevance *= 1.3
                    elif any(e.lower() in query_lower for e in entry.entities):
                        base_relevance *= 1.2
                scored.append((entry, base_relevance))

            scored.sort(key=lambda x: x[1], reverse=True)

            working_lines = []
            for entry, _ in scored:
                if token_count + entry.estimate_tokens() <= max_tokens:
                    working_lines.append(entry.to_markdown_line())
                    token_count += entry.estimate_tokens()
                    entry.access()

            if working_lines:
                parts.append("## Working Memory\n" + "\n".join(working_lines))

        # Recent entities
        recent_entities = self.entity_tracker.get_recent_entities(days=14, limit=5)
        if recent_entities:
            entity_lines = [f"- {e.name} ({e.entity_type}): {e.context}" for e in recent_entities if e.context]
            if entity_lines:
                parts.append("## Recent Entities\n" + "\n".join(entity_lines))

        if parts:
            self.mark_dirty()

        return "\n\n".join(parts) if parts else ""

    def append_history(self, entry: str) -> None:
        """Append entry to HISTORY.md with rotation."""
        # Check size and rotate if needed
        if self.history_file.exists():
            size_mb = self.history_file.stat().st_size / (1024 * 1024)
            if size_mb > MEMORY_CONFIG["max_history_size_mb"]:
                self._rotate_history()

        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def _rotate_history(self) -> None:
        """Rotate HISTORY.md to archive."""
        if not self.history_file.exists():
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = self.history_file.with_suffix(f".{timestamp}.md")

        try:
            shutil.move(str(self.history_file), str(archive_path))
            logger.info("Rotated history to {}", archive_path.name)

            # Keep only last 5 archives
            archives = sorted(self.memory_dir.glob("HISTORY.*.md"))
            for old_archive in archives[:-5]:
                old_archive.unlink()
                logger.debug("Removed old archive {}", old_archive.name)
        except Exception as e:
            logger.warning("Failed to rotate history: {}", e)

    def prune_low_relevance(self, threshold: float, max_entries: int) -> int:
        """Remove entries below relevance threshold."""
        initial_count = len(self.entries)

        # Keep CORE and above threshold
        self.entries = [
            e for e in self.entries
            if e.layer == MemoryLayer.CORE or e.compute_relevance() >= threshold
        ]

        # If over limit, keep top by relevance
        if len(self.entries) > max_entries:
            core = [e for e in self.entries if e.layer == MemoryLayer.CORE]
            non_core = [e for e in self.entries if e.layer != MemoryLayer.CORE]
            non_core.sort(key=lambda e: e.compute_relevance(), reverse=True)
            keep_count = max_entries - len(core)
            self.entries = core + non_core[:keep_count]

        return initial_count - len(self.entries)

    def auto_cleanup(self) -> dict[str, int]:
        """Automatic cleanup. Returns counts of removed items."""
        results = {}

        # Prune entries
        entries_removed = self.prune_low_relevance(
            MEMORY_CONFIG["min_relevance_threshold"],
            MEMORY_CONFIG["max_entries"],
        )
        if entries_removed > 0:
            results["entries_removed"] = entries_removed

        # Track entities before pruning
        entity_names_before = set(self.entity_tracker.entities.keys())
        entities_removed = self.entity_tracker.prune_inactive(
            MEMORY_CONFIG["entity_inactivity_days"],
            MEMORY_CONFIG["max_entities"],
        )
        if entities_removed > 0:
            results["entities_removed"] = entities_removed

            # Clean up dangling entity references in entries
            valid_entities = set(self.entity_tracker.entities.keys())
            removed_entities = entity_names_before - valid_entities
            if removed_entities:
                for entry in self.entries:
                    entry.entities = [e for e in entry.entities if e in valid_entities]
                logger.debug("Cleaned {} dangling entity references", len(removed_entities))

        # Check file sizes
        for file_path in [self.entries_file, self.entities_file]:
            if file_path.exists():
                size_mb = file_path.stat().st_size / (1024 * 1024)
                if size_mb > MEMORY_CONFIG["max_file_size_mb"]:
                    logger.warning(
                        "Memory file {} is {:.1f}MB (limit: {}MB)",
                        file_path.name, size_mb, MEMORY_CONFIG["max_file_size_mb"]
                    )
                    results[f"{file_path.name}_size_mb"] = round(size_mb, 2)

        if results:
            self.mark_dirty()

        return results

    def promote_to_core(self, entry_id: str) -> bool:
        """Promote an entry to CORE layer."""
        for entry in self.entries:
            if entry.id == entry_id:
                entry.layer = MemoryLayer.CORE
                entry.importance = max(entry.importance, 0.8)
                self.mark_dirty()
                return True
        return False

    def demote_to_episodic(self, entry_id: str) -> bool:
        """Demote an entry to EPISODIC layer."""
        for entry in self.entries:
            if entry.id == entry_id:
                entry.layer = MemoryLayer.EPISODIC
                self.mark_dirty()
                return True
        return False

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into structured memory entries."""
        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation: {} to consolidate", len(old_messages))

        # Build conversation text
        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            content = m["content"]
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {content}")

        conversation_text = "\n".join(lines)
        existing_summary = self._build_summary_for_consolidation()

        prompt = f"""Analyze this conversation and extract important memories.

## Current Memory Summary
{existing_summary or "(empty)"}

## Conversation to Process
{conversation_text}

Extract memories and call save_memory tool. Each entry should be concise (1-3 sentences) with proper category."""

        try:
            response = await provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Extract structured memories from conversations. Call the save_memory tool."},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory")
                return False

            args = response.tool_calls[0].arguments

            # Robust JSON parsing
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    try:
                        args = repair_json(args, return_objects=True)
                        logger.debug("Memory consolidation: repaired malformed JSON")
                    except Exception as e:
                        logger.warning("Memory consolidation: JSON parse failed: {}", str(e)[:100])
                        return False

            if isinstance(args, list) and args and isinstance(args[0], dict):
                args = args[0]

            if not isinstance(args, dict):
                logger.warning("Memory consolidation: unexpected arguments type")
                return False

            # Process entries
            entries_data = args.get("entries", [])
            if isinstance(entries_data, list):
                for entry_data in entries_data:
                    if not isinstance(entry_data, dict):
                        continue
                    content = entry_data.get("content", "")
                    if not content or not content.strip():
                        continue

                    category_str = entry_data.get("category", "fact")
                    try:
                        category = MemoryCategory(category_str)
                    except ValueError:
                        category = MemoryCategory.FACT

                    # Check duplicates
                    content_lower = content.lower()
                    is_duplicate = any(
                        content_lower in existing.content.lower() or
                        existing.content.lower() in content_lower
                        for existing in self.entries
                    )

                    if not is_duplicate:
                        entry = self._create_entry(
                            content=content.strip(),
                            category=category,
                            importance=entry_data.get("importance"),
                            tags=entry_data.get("tags", []),
                            entities=entry_data.get("entities", []),
                        )
                        self.entries.append(entry)

                        # Update entity tracker (entities extracted by AI)
                        for entity_name in entry.entities:
                            self.entity_tracker.add_or_update(entity_name, entity_type="topic", context=content[:100])

            # Save history entry
            if history_entry := args.get("history_entry"):
                if not isinstance(history_entry, str):
                    history_entry = json.dumps(history_entry, ensure_ascii=False)
                self.append_history(history_entry)

            # Update session
            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count

            # Auto cleanup
            cleanup_results = self.auto_cleanup()
            if cleanup_results:
                logger.info("Memory cleanup: {}", cleanup_results)

            # Save now (don't wait for debounce)
            self._save_now()

            logger.info("Memory consolidation done: {} entries, last_consolidated={}", len(self.entries), session.last_consolidated)
            return True

        except Exception:
            logger.exception("Memory consolidation failed")
            return False

    def _build_summary_for_consolidation(self) -> str:
        """Build brief summary for consolidation context."""
        lines = []
        for category in [MemoryCategory.IDENTITY, MemoryCategory.PREFERENCE, MemoryCategory.PROJECT]:
            entries = self.get_entries_by_category(category)[:3]
            if entries:
                lines.append(f"{category.value}: " + "; ".join(e.content for e in entries))
        return "\n".join(lines) if lines else ""

    # Legacy compatibility
    def read_long_term(self) -> str:
        """Legacy: read MEMORY.md content."""
        if self.memory_md_file.exists():
            return self.memory_md_file.read_text(encoding="utf-8")
        self._sync_to_memory_md()
        return self.memory_md_file.read_text(encoding="utf-8") if self.memory_md_file.exists() else ""

    def write_long_term(self, content: str) -> None:
        """Legacy: parse and add entries from Markdown."""
        for line in content.split("\n"):
            if line.startswith("- ") and not line.startswith("- #"):
                item = line[2:].strip()
                if item.startswith("★ "):
                    item = item[2:]
                if item:
                    self.add_entry(item, MemoryCategory.FACT)

    def flush(self) -> None:
        """Force immediate save (call before shutdown)."""
        if self._dirty:
            self._save_now()
