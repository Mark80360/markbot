"""Tiered Memory System for markbot.

Provides L1-L4 layered memory architecture inspired by Swarmbot.

Usage:
    from markbot.agent.tiered_memory import TieredMemoryManager
    
    memory = TieredMemoryManager(workspace_path)
    
    # In Agent Loop
    context = memory.assemble_context(chat_id, user_input)
    
    # After Loop
    memory.save_turn(chat_id, user_input, assistant_response)
    memory.clear_whiteboard(chat_id)
"""

from .base import MemoryTier, MemoryEntry
from .whiteboard import WhiteboardMemory
from .hot_memory import HotMemory
from .warm_memory import WarmMemory
from .cold_memory import ColdMemory
from .manager import TieredMemoryManager

__all__ = [
    "MemoryTier",
    "MemoryEntry", 
    "WhiteboardMemory",
    "HotMemory",
    "WarmMemory",
    "ColdMemory",
    "TieredMemoryManager",
]
