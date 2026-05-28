"""Memory plugin discovery — find and instantiate external memory providers.

Supports three discovery mechanisms:
  1. Entry points: packages declaring `markbot.memory_providers` in pyproject.toml
  2. Naming convention: installed packages matching `markbot_memory_*` or `markbot-memory-*`
  3. Manual registration via register()

Only one external provider is active at a time.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Any, Dict, Optional, Type

from loguru import logger

from markbot.memory.provider import MemoryProvider


@dataclass
class MemoryProviderInfo:
    """Metadata about a discovered memory provider."""

    name: str
    description: str = ""
    package_name: str = ""
    provider_class: type | None = None
    is_available: bool = True


class MemoryPluginDiscovery:
    """Discovers and manages external memory provider plugins.

    Usage:
        discovery = MemoryPluginDiscovery()
        providers = discovery.discover()
        provider = discovery.create_provider("chroma", config={"host": "localhost"})
    """

    ENTRY_POINT_GROUP = "markbot.memory_providers"

    def __init__(self):
        self._registered: Dict[str, Type[MemoryProvider]] = {}
        self._discovered: Dict[str, MemoryProviderInfo] = {}

    def discover(self) -> list[MemoryProviderInfo]:
        """Discover all available memory providers.

        Searches via entry points and naming convention.
        Returns a list of MemoryProviderInfo for each discovered provider.
        """
        self._discovered.clear()

        # 1. Entry points (highest priority)
        self._discover_entry_points()

        # 2. Naming convention
        self._discover_by_naming()

        # 3. Include manually registered
        for name, cls in self._registered.items():
            if name not in self._discovered:
                self._discovered[name] = MemoryProviderInfo(
                    name=name,
                    description=cls.__doc__ or "",
                    provider_class=cls,
                )

        return list(self._discovered.values())

    def register(self, name: str, provider_class: Type[MemoryProvider]) -> None:
        """Manually register a memory provider.

        Args:
            name: Short identifier for the provider.
            provider_class: A subclass of MemoryProvider.
        """
        if not issubclass(provider_class, MemoryProvider):
            raise TypeError(f"{provider_class} is not a subclass of MemoryProvider")
        self._registered[name] = provider_class
        logger.info("Registered memory provider: {}", name)

    def create_provider(
        self,
        name: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> MemoryProvider | None:
        """Instantiate a memory provider by name.

        Args:
            name: Provider name (as returned by discover()).
            config: Configuration dict passed to the provider constructor.

        Returns:
            An instance of the provider, or None if not found/instantiation failed.
        """
        # Check manually registered first
        cls = self._registered.get(name)
        if cls is None:
            # Check discovered info
            info = self._discovered.get(name)
            if info and info.provider_class:
                cls = info.provider_class
            else:
                # Try to import dynamically
                cls = self._try_import(name)

        if cls is None:
            logger.warning("Memory provider '{}' not found", name)
            return None

        try:
            instance = cls()
            if config:
                # Store config for the provider to use during initialize()
                # Providers should read this via kwargs in initialize() or
                # access self._plugin_config if set.
                instance._plugin_config = config  # type: ignore[attr-defined]
            return instance
        except Exception as e:
            logger.error("Failed to instantiate memory provider '{}': {}", name, e)
            return None

    def list_available(self) -> list[str]:
        """List names of all discovered providers."""
        if not self._discovered:
            self.discover()
        return list(self._discovered.keys())

    # -- Internal discovery methods ------------------------------------------

    def _discover_entry_points(self) -> None:
        """Discover providers via Python entry points."""
        try:
            eps = importlib.metadata.entry_points()
            # Python 3.12+ returns a SelectableGroups; 3.9 returns dict
            if hasattr(eps, "select"):
                group_eps = eps.select(group=self.ENTRY_POINT_GROUP)
            elif isinstance(eps, dict):
                group_eps = eps.get(self.ENTRY_POINT_GROUP, [])
            else:
                group_eps = getattr(eps, self.ENTRY_POINT_GROUP, [])

            for ep in group_eps:
                try:
                    cls = ep.load()
                    if isinstance(cls, type) and issubclass(cls, MemoryProvider):
                        name = ep.name
                        self._discovered[name] = MemoryProviderInfo(
                            name=name,
                            description=cls.__doc__ or "",
                            package_name=ep.dist.name if ep.dist else "",
                            provider_class=cls,
                        )
                        logger.debug("Discovered memory provider via entry point: {}", name)
                except Exception as e:
                    logger.debug("Failed to load entry point {}: {}", ep.name, e)
        except Exception as e:
            logger.debug("Entry point discovery failed: {}", e)

    def _discover_by_naming(self) -> None:
        """Discover providers by scanning installed packages."""
        prefix = "markbot_memory_"

        try:
            for dist in importlib.metadata.distributions():
                name = dist.metadata["Name"]
                if not name:
                    continue

                normalized = name.lower().replace("-", "_")
                if normalized.startswith(prefix):
                    provider_name = normalized[len(prefix):]
                    if provider_name and provider_name not in self._discovered:
                        cls = self._try_import(normalized)
                        if cls:
                            self._discovered[provider_name] = MemoryProviderInfo(
                                name=provider_name,
                                description=cls.__doc__ or "",
                                package_name=name,
                                provider_class=cls,
                            )
                            logger.debug("Discovered memory provider by naming: {}", provider_name)
        except Exception as e:
            logger.debug("Naming convention discovery failed: {}", e)

    def _try_import(self, package_or_name: str) -> type | None:
        """Try to import a MemoryProvider subclass from a package."""
        module_names = [
            f"markbot_memory_{package_or_name}",
            f"markbot_memory_{package_or_name.replace('-', '_')}",
            package_or_name,
        ]

        for module_name in module_names:
            try:
                module = importlib.import_module(module_name)
                # Look for a MemoryProvider subclass
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, MemoryProvider)
                        and attr is not MemoryProvider
                    ):
                        return attr
            except ImportError:
                continue
            except Exception as e:
                logger.debug("Error importing {}: {}", module_name, e)

        return None


__all__ = ["MemoryPluginDiscovery", "MemoryProviderInfo"]
