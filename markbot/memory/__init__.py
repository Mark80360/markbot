"""Memory system for markbot.

Provides file-based memory management with:
- MemoryManager for conversation memory and context compression
- BaseMemoryManager interface with lifecycle hooks
- MemoryStore for persistent curated memory (add/replace/remove)
- MemorySecurityScanner for injection/exfiltration detection
- ContextFencing for memory-context tags and streaming scrubber
- MemoryProvider ABC for pluggable memory backends
- DailyLogManager for lightweight interaction logging
- MemoryEncoder for active preference detection and encoding
"""

from markbot.agent.hooks import BootstrapHook, MemoryCompactionHook

from .base import BaseMemoryManager
from .daily_log import DailyLogManager
from .encoder import MemoryEncoder
from .fencing import (
    MEMORY_CONTEXT_CLOSE,
    MEMORY_CONTEXT_OPEN,
    StreamingContextScrubber,
    fence_context,
    is_fenced,
    sanitize_context,
)
from .manager import MemoryManager, redact_sensitive_text
from .provider import MemoryProvider
from .scanner import MemorySecurityScanner
from .tool import MemoryStore

__all__ = [
    "BaseMemoryManager",
    "DailyLogManager",
    "MemoryManager",
    "redact_sensitive_text",
    "MemoryEncoder",
    "MemorySecurityScanner",
    "MemoryProvider",
    "MemoryStore",
    "BootstrapHook",
    "MemoryCompactionHook",
    "fence_context",
    "sanitize_context",
    "is_fenced",
    "StreamingContextScrubber",
    "MEMORY_CONTEXT_OPEN",
    "MEMORY_CONTEXT_CLOSE",
]
