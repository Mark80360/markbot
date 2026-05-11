"""Memory system for markbot architecture.

Provides ReMeLight-backed memory management with:
- Abstract BaseMemoryManager interface
- ReMeLightMemoryManager concrete implementation
- Bootstrap hook for first-time user guidance
- Memory compaction hook for context window management
- DailyLogManager for lightweight interaction logging
- MemoryEncoder for active preference detection and encoding

Architecture ported from tiered memory system.
"""

from markbot.agent.hooks import BootstrapHook, MemoryCompactionHook

from .base import BaseMemoryManager
from .daily_log import DailyLogManager
from .encoder import MemoryEncoder
from .manager import ReMeLightMemoryManager

__all__ = [
    "BaseMemoryManager",
    "DailyLogManager",
    "ReMeLightMemoryManager",
    "MemoryEncoder",
    "BootstrapHook",
    "MemoryCompactionHook",
]
