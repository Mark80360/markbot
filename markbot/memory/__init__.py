"""Memory system for markbot architecture.

Provides ReMeLight-backed memory management with:
- Abstract BaseMemoryManager interface
- ReMeLightMemoryManager concrete implementation (ported from CoPaw)
- Bootstrap hook for first-time user guidance
- Memory compaction hook for context window management
- AgentMdManager for markdown file operations
- DailyLogManager for lightweight interaction logging

Architecture ported from CoPaw's tiered memory system.
"""

from .base import BaseMemoryManager
from .daily_log import DailyLogManager
from .manager import ReMeLightMemoryManager, _MessageWrapper
from .md_manager import AgentMdManager
from .hooks import BootstrapHook, MemoryCompactionHook

__all__ = [
    "BaseMemoryManager",
    "DailyLogManager",
    "ReMeLightMemoryManager",
    "_MessageWrapper",
    "AgentMdManager",
    "BootstrapHook",
    "MemoryCompactionHook",
]
