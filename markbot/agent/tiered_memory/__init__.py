"""Tiered Memory System for markbot - Complete Rewrite v2.0.

Provides a comprehensive L1-L4 layered memory architecture with:
- L1 Whiteboard: Loop-level temporary workspace with checkpointing
- L1.5 Session: Sliding window context (managed implicitly)
- L2 Hot Memory: High-value facts with strict quality gate
- L3 Warm Memory: 100% fidelity daily audit logs  
- L4 Cold Memory: Long-term semantic storage with vector search

Quality Gate: MemorySanitizer (3-stage pipeline)
1. Noise Filter: Reject conversational filler and low-value content
2. Secret Redaction: Auto-redact API keys, tokens, passwords
3. Deduplication: Reject near-duplicates (>85% similarity)

Architecture Inspired by: Swarmbot, CoPaw's ReMeLight

Usage:
    from markbot.agent.tiered_memory import MemoryManager
    
    # Initialize
    manager = MemoryManager(workspace_path="/path/to/workspace")
    
    # Create session
    whiteboard = manager.create_session(chat_id="session_001")
    
    # Process dialogue turns
    manager.process_turn(
        chat_id="session_001",
        user_input="Help me refactor this code",
        assistant_response="I'll help you refactor...",
        turn_number=1
    )
    
    # Get context for prompt injection
    context = manager.get_full_context(
        chat_id="session_001",
        query="refactoring"
    )
    
    # Close session (auto-extracts to persistent layers)
    summary = manager.close_session(chat_id="session_001")
    
    # Search across all memory layers
    results = manager.search_memory(query="database optimization")
"""

# Core data models and types
from .base import (
    MemoryTier,                    # Enum: WHITEBOARD(1), SESSION(2), HOT(3), WARM(4), COLD(5)
    HotMemoryCategory,             # Enum: 9 categories for L2 entries
    KnowledgeType,                 # Enum: 7 knowledge types for L4 entries
    LoopState,                     # Enum: Whiteboard loop states
    HotMemoryEntry,                # Dataclass: Structured L2 entry
    ColdMemoryDocument,            # Dataclass: Structured L4 document
    ExecutionStep,                 # Dataclass: Task execution step
    SubtaskRecord,                 # Dataclass: Completed subtask record
    MemoryContext,                 # Dataclass: Context assembly config
)

# Memory layer implementations
from .whiteboard import WhiteboardMemory           # L1: Loop-level workspace
from .hot_memory import HotMemory                  # L2: Short-term persistent
from .warm_memory import WarmMemory                # L3: Daily audit logs
from .cold_memory import ColdMemory, ColdMemoryEntry  # L4: Long-term semantic

# Quality gate
from .memory_sanitizer import MemorySanitizer      # 3-stage quality pipeline

# Central coordinator
from .manager import MemoryManager                 # Main orchestrator

# Legacy alias for backward compatibility
TieredMemoryManager = MemoryManager

__version__ = "2.0.0"
__author__ = "markbot"

__all__ = [
    # Core types
    "MemoryTier",
    "HotMemoryCategory", 
    "KnowledgeType",
    "LoopState",
    "HotMemoryEntry",
    "ColdMemoryEntry",
    "ExecutionStep",
    "SubtaskRecord",
    "MemoryContext",
    
    # Memory layers
    "WhiteboardMemory",
    "HotMemory",
    "WarmMemory",
    "ColdMemory",
    
    # Quality gate
    "MemorySanitizer",
    
    # Manager
    "MemoryManager",
    "TieredMemoryManager",  # Legacy alias
]
