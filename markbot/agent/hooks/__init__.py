"""Memory hooks for Markbot — bootstrap and compaction."""

from .bootstrap import BootstrapHook
from .compaction import MemoryCompactionHook

__all__ = [
    "BootstrapHook",
    "MemoryCompactionHook",
]
