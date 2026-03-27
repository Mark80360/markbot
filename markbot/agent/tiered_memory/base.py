"""Base classes for tiered memory system."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from enum import Enum


class MemoryTier(Enum):
    """Memory tier levels."""
    WHITEBOARD = 1    # L1: Loop-level temporary workspace
    SESSION = 2       # L1.5: Session-level (8-turn sliding window)
    HOT = 3           # L2: Short-term persistent (20 entries max)
    WARM = 4          # L3: Daily logs
    COLD = 5          # L4: Long-term semantic storage


@dataclass
class MemoryEntry:
    """A single memory entry."""
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    tier: MemoryTier = MemoryTier.HOT
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert entry to dictionary."""
        return {
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "tier": self.tier.value
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        """Create entry from dictionary."""
        return cls(
            content=data["content"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            metadata=data.get("metadata", {}),
            tier=MemoryTier(data.get("tier", 3))
        )


class BaseMemoryLayer(ABC):
    """Abstract base class for memory layers."""
    
    def __init__(self, tier: MemoryTier, workspace_path: str):
        self.tier = tier
        self.workspace_path = workspace_path
    
    @abstractmethod
    def add(self, content: str, **metadata) -> None:
        """Add an entry to this memory layer."""
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


class CompactResult:
    """Result of memory compaction."""
    
    def __init__(self, archived: List[Dict], kept: int, chat_id: str):
        self.archived = archived  # Entries moved to higher tier
        self.kept = kept          # Entries remaining
        self.chat_id = chat_id
        self.timestamp = datetime.now()
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "archived": self.archived,
            "kept": self.kept,
            "chat_id": self.chat_id,
            "timestamp": self.timestamp.isoformat()
        }
