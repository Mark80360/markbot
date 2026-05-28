"""Memory plugin discovery and registration.

Enables external memory providers to be discovered via:
1. Python package naming convention (markbot_memory_*)
2. Entry points (markbot.memory_providers)
3. Manual registration via register()
"""

from markbot.memory.plugins.discovery import MemoryPluginDiscovery, MemoryProviderInfo

__all__ = ["MemoryPluginDiscovery", "MemoryProviderInfo"]
