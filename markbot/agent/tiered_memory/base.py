"""Base classes for tiered memory system.

Defines core data models, enumerations, and abstract interfaces
for the L1-L4 layered memory architecture.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union


class MemoryTier(Enum):
    """Memory tier levels in the hierarchical architecture."""
    WHITEBOARD = 1    # L1: Loop-level temporary workspace
    SESSION = 2       # L1.5: Session-level (sliding window)
    HOT = 3           # L2: Short-term persistent (high-value facts)
    WARM = 4          # L3: Daily audit logs
    COLD = 5          # L4: Long-term semantic storage


class LoopState(Enum):
    """Agent loop execution states."""
    INITIAL = "INITIAL"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    REVIEW = "REVIEW"
    DONE = "DONE"
    FAILED = "FAILED"
    WAITING = "WAITING"


class HotMemoryCategory(Enum):
    """Strict categories for Hot Memory entries (L2)."""
    FILE_OP = "FILE_OP"           # File operations (create/modify/delete)
    DECISION = "DECISION"         # Technical decisions
    COMPLETION = "COMPLETION"     # Task completion status
    FIX = "FIX"                   # Bug fixes and error resolutions
    WARNING = "WARNING"           # Important warnings and notes
    USER_INTENT = "USER_INTENT"   # Recorded user requirements and preferences
    NEXT_STEP = "NEXT_STEP"       # Planned next actions
    TURN_FACT = "TURN_FACT"       # Auto-extracted facts from conversation turns
    SESSION_SUMMARY = "SESSION_SUMMARY"  # Summaries from session compaction


class KnowledgeType(Enum):
    """Knowledge type classification for Cold Memory (L4)."""
    FACT = "FACT"                         # Objective facts
    EXPERIENCE = "EXPERIENCE"             # Lessons learned
    PREFERENCE = "PREFERENCE"             # User preferences
    DECISION = "DECISION"                 # Decision records
    ERROR_PATTERN = "ERROR_PATTERN"       # Error patterns and solutions
    CODE_PATTERN = "CODE_PATTERN"         # Code patterns and idioms
    PROJECT_CONTEXT = "PROJECT_CONTEXT"   # Project-specific context


@dataclass
class MemoryEntry:
    """A single memory entry with full metadata tracking."""
    entry_id: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    tier: MemoryTier = MemoryTier.HOT
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert entry to dictionary."""
        return {
            "entry_id": self.entry_id,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "tier": self.tier.value
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        """Create entry from dictionary."""
        return cls(
            entry_id=data.get("entry_id", ""),
            content=data["content"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            metadata=data.get("metadata", {}),
            tier=MemoryTier(data.get("tier", 3))
        )


@dataclass
class HotMemoryEntry:
    """Enhanced entry for Hot Memory (L2) with strict quality attributes."""
    entry_id: str
    content: str
    category: HotMemoryCategory
    confidence: float = 0.8                    # 0-1 extraction confidence
    source_turn: int = 0                       # Source conversation turn
    source_session: str = ""                   # Source session ID
    created_at: datetime = field(default_factory=datetime.now)
    access_count: int = 0                      # Access frequency for LRU
    tags: List[str] = field(default_factory=list)  # Custom tags
    
    def to_markdown(self) -> str:
        """Generate standard Markdown format for storage."""
        emoji_map = {
            HotMemoryCategory.FILE_OP: "📁",
            HotMemoryCategory.DECISION: "💡",
            HotMemoryCategory.COMPLETION: "✅",
            HotMemoryCategory.FIX: "🐛",
            HotMemoryCategory.WARNING: "⚠️",
            HotMemoryCategory.USER_INTENT: "👤",
            HotMemoryCategory.NEXT_STEP: "➡️",
            HotMemoryCategory.TURN_FACT: "📝",
            HotMemoryCategory.SESSION_SUMMARY: "📚",
        }
        
        emoji = emoji_map.get(self.category, "📌")
        timestamp_str = self.created_at.strftime("%Y-%m-%d %H:%M")
        
        return (
            f"- [{timestamp_str}] {emoji} `{self.category.value}` "
            f"{self.content}\n"
            f"  _来源: Turn #{self.source_turn} | "
            f"置信度: {self.confidence:.2f}_"
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "entry_id": self.entry_id,
            "content": self.content,
            "category": self.category.value,
            "confidence": self.confidence,
            "source_turn": self.source_turn,
            "source_session": self.source_session,
            "created_at": self.created_at.isoformat(),
            "access_count": self.access_count,
            "tags": self.tags
        }


@dataclass
class ExecutionStep:
    """A single step in the execution plan (for Whiteboard L1)."""
    step_id: str
    description: str
    status: Literal["PENDING", "IN_PROGRESS", "COMPLETED", "FAILED"] = "PENDING"
    result: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)  # DAG dependency support
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "status": self.status,
            "result": self.result,
            "dependencies": self.dependencies
        }


@dataclass
class SubtaskRecord:
    """Record of a completed subtask (for Whiteboard L1)."""
    task: str
    result: str
    timestamp: float = field(default_factory=lambda: __import__('time').time())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "result": self.result,
            "timestamp": self.timestamp
        }


@dataclass
class ColdMemoryDocument:
    """Enhanced document structure for Cold Memory (L4)."""
    doc_id: str
    content: str
    knowledge_type: KnowledgeType
    source_session: str = ""
    source_turn: int = 0
    extraction_method: Literal[
        "auto_extract", "llm_summarize", 
        "manual_add", "compact_archive"
    ] = "auto_extract"
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed_at: datetime = field(default_factory=datetime.now)
    access_count: int = 0
    related_docs: List[str] = field(default_factory=list)
    contradicts: List[str] = field(default_factory=list)
    confidence: float = 0.8
    verified: bool = False
    expiration_date: Optional[datetime] = None
    tags: List[str] = field(default_factory=list)
    
    def format_for_embedding(self) -> str:
        """
        Optimize text for better vector representation.
        
        Strategy:
        - Add type prefix (helps model understand semantic category)
        - Remove noise markers (timestamps, IDs)
        - Keep within optimal length range (200-800 chars)
        """
        type_prefix = {
            KnowledgeType.FACT: "[Fact]",
            KnowledgeType.EXPERIENCE: "[Lesson Learned]",
            KnowledgeType.PREFERENCE: "[User Preference]",
            KnowledgeType.DECISION: "[Decision Made]",
            KnowledgeType.ERROR_PATTERN: "[Error Pattern]",
            KnowledgeType.CODE_PATTERN: "[Code Pattern]",
            KnowledgeType.PROJECT_CONTEXT: "[Project Context]"
        }.get(self.knowledge_type, "[Knowledge]")
        
        import re
        cleaned = re.sub(r'\[.*?(REDACTED|SANITIZED).*?\]', '', self.content)
        cleaned = re.sub(r'\d{4}-\d{2}-\d{2}[\s\T:]*', '', cleaned)
        cleaned = cleaned.strip()
        
        if len(cleaned) > 800:
            cleaned = cleaned[:800] + "..."
        
        return f"{type_prefix} {cleaned}"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "content": self.content,
            "knowledge_type": self.knowledge_type.value,
            "source_session": self.source_session,
            "source_turn": self.source_turn,
            "extraction_method": self.extraction_method,
            "created_at": self.created_at.isoformat(),
            "confidence": self.confidence,
            "verified": self.verified,
            "tags": self.tags
        }


class BaseMemoryLayer(ABC):
    """Abstract base class for all memory layers."""
    
    def __init__(self, tier: MemoryTier, workspace_path: str):
        self.tier = tier
        self.workspace_path = workspace_path
        self._initialized = False
    
    @abstractmethod
    def add(self, content: str, **metadata) -> bool:
        """
        Add an entry to this memory layer.
        
        Returns:
            True if entry was accepted, False if rejected
        """
        pass
    
    @abstractmethod
    def get_context(self, query: Optional[str] = None, limit: int = 10) -> str:
        """Get context from this layer for prompt injection."""
        pass
    
    @abstractmethod
    def clear(self) -> None:
        """Clear this memory layer."""
        pass
    
    @property
    @abstractmethod
    def is_persistent(self) -> bool:
        """Whether this layer persists to disk."""
        pass
    
    @property
    def is_available(self) -> bool:
        """Whether this layer is operational."""
        return self._initialized


class CompactResult:
    """Result of session memory compaction."""
    
    def __init__(
        self, 
        archived: List[Dict], 
        kept: int, 
        chat_id: str,
        summary: Optional[str] = None,
        extraction_stats: Optional[Dict[str, int]] = None
    ):
        self.archived = archived          # Entries moved to higher tiers
        self.kept = kept                  # Entries remaining in session
        self.chat_id = chat_id
        self.timestamp = datetime.now()
        self.summary = summary            # Generated structured summary
        self.extraction_stats = extraction_stats or {
            "decisions": 0,
            "file_ops": 0,
            "fixes": 0,
            "user_intents": 0
        }
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "archived": self.archived,
            "kept": self.kept,
            "chat_id": self.chat_id,
            "timestamp": self.timestamp.isoformat(),
            "summary": self.summary,
            "extraction_stats": self.extraction_stats
        }


class MemoryContext:
    """Assembled memory context for prompt injection."""
    
    def __init__(
        self,
        session_context: str = "",
        hot_context: str = "",  
        warm_context: str = "",
        cold_context: str = "",
        whiteboard_context: str = ""
    ):
        self.session_context = session_context
        self.hot_context = hot_context
        self.warm_context = warm_context
        self.cold_context = cold_context
        self.whiteboard_context = whiteboard_context
    
    def to_prompt(self) -> str:
        """Format all context for system prompt injection."""
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
        """Check if context is completely empty."""
        return not any([
            self.session_context,
            self.hot_context,
            self.warm_context,
            self.cold_context,
            self.whiteboard_context
        ])
    
    def total_chars(self) -> int:
        """Calculate total character count of all contexts."""
        return sum(len(ctx) for ctx in [
            self.whiteboard_context,
            self.session_context,
            self.hot_context,
            self.warm_context,
            self.cold_context
        ])
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_context": self.session_context,
            "hot_context": self.hot_context,
            "warm_context": self.warm_context,
            "cold_context": self.cold_context,
            "whiteboard_context": self.whiteboard_context,
            "total_chars": self.total_chars(),
            "is_empty": self.is_empty()
        }
