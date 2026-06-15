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
- MemoryPluginDiscovery for external memory provider discovery

Long-term (semantic vector) memory:
- Embedder / build_embedder: layered text→vector encoding (OpenAI API,
  local sentence-transformers, or zero-dependency hashing fallback)
- VectorStore / SQLiteVectorStore / InMemoryVectorStore: persistent
  vector storage with cosine ranking
- ChromaVectorStore: optional ChromaDB backend
- LongTermMemory: facade that indexes turns/notes/delegations and
  recalls them by meaning (not just keywords)
- Consolidator: periodic dedup + importance-decay + promotion
- build_vectorstore: factory selecting the configured backend
"""

from markbot.agent.hooks import BootstrapHook, MemoryCompactionHook

from .base import BaseMemoryManager
from .consolidation import (
    ConsolidationConfig,
    ConsolidationReport,
    Consolidator,
)
from .daily_log import DailyLogManager
from .embedder import (
    DEFAULT_ST_MODEL,
    HASHING_DIM,
    Embedder,
    HashingEmbedder,
    OpenAICompatibleEmbedder,
    SentenceTransformerEmbedder,
    build_embedder,
)
from .encoder import MemoryEncoder
from .fencing import (
    MEMORY_CONTEXT_CLOSE,
    MEMORY_CONTEXT_OPEN,
    StreamingContextScrubber,
    fence_context,
    is_fenced,
    sanitize_context,
)
from .longterm import LongTermConfig, LongTermMemory
from .manager import MemoryManager, redact_sensitive_text
from .plugins.discovery import MemoryPluginDiscovery, MemoryProviderInfo
from .provider import MemoryProvider
from .scanner import MemorySecurityScanner
from .tool import MemoryStore
from .vectorstore import (
    InMemoryVectorStore,
    SQLiteVectorStore,
    VectorRecord,
    VectorStore,
)
from .vectorstore_factory import build_vectorstore

__all__ = [
    "BaseMemoryManager",
    "DailyLogManager",
    "MemoryManager",
    "redact_sensitive_text",
    "MemoryEncoder",
    "MemorySecurityScanner",
    "MemoryProvider",
    "MemoryPluginDiscovery",
    "MemoryProviderInfo",
    "MemoryStore",
    "BootstrapHook",
    "MemoryCompactionHook",
    "fence_context",
    "sanitize_context",
    "is_fenced",
    "StreamingContextScrubber",
    "MEMORY_CONTEXT_OPEN",
    "MEMORY_CONTEXT_CLOSE",
    # Long-term (vector) memory
    "Embedder",
    "HashingEmbedder",
    "SentenceTransformerEmbedder",
    "OpenAICompatibleEmbedder",
    "build_embedder",
    "HASHING_DIM",
    "DEFAULT_ST_MODEL",
    "VectorStore",
    "VectorRecord",
    "SQLiteVectorStore",
    "InMemoryVectorStore",
    "build_vectorstore",
    "LongTermMemory",
    "LongTermConfig",
    "Consolidator",
    "ConsolidationConfig",
    "ConsolidationReport",
]
