"""Memory hooks for markbot — bootstrap and compaction."""

from .bootstrap import BootstrapHook
from .compaction import MemoryCompactionHook

__all__ = [
    "BootstrapHook",
    "MemoryCompactionHook",
]
