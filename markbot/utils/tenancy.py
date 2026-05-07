"""Multi-tenancy support — TenantContext and tenant-aware resource isolation.

Provides per-tenant context propagation so that a single markbot
instance can serve multiple organizations or users with isolated
sessions, budgets, and rate limits.

Usage::

    from markbot.utils.tenancy import TenantContext, set_tenant, get_tenant

    ctx = TenantContext(tenant_id="acme-corp", tier="enterprise")
    set_tenant(ctx)

    # Later, in any module:
    tenant = get_tenant()
    session_key = tenant.scoped_key("cli:direct")
    # → "acme-corp:cli:direct"
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class TenantQuota:
    """Per-tenant resource quotas."""

    max_sessions: int = 100
    max_budget_usd: float | None = None
    rate_limit_rpm: int = 60
    max_context_window_tokens: int = 65_536
    max_tool_iterations: int = 40
    allowed_tools: list[str] = field(default_factory=lambda: ["*"])
    denied_tools: list[str] = field(default_factory=list)
    allowed_providers: list[str] = field(default_factory=lambda: ["*"])
    allowed_channels: list[str] = field(default_factory=lambda: ["*"])


@dataclass
class TenantContext:
    """Immutable tenant context propagated via contextvars.

    Carries tenant identity, tier, and quota information through
    the call stack without explicit parameter passing.
    """

    tenant_id: str
    tier: str = "free"
    display_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    quota: TenantQuota = field(default_factory=TenantQuota)

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.tenant_id

    def scoped_key(self, key: str) -> str:
        """Prefix a key with the tenant ID for namespace isolation.

        Example: scoped_key("cli:direct") → "acme-corp:cli:direct"
        """
        return f"{self.tenant_id}:{key}"

    def unscoped_key(self, scoped_key: str) -> str:
        """Remove the tenant prefix from a scoped key."""
        prefix = f"{self.tenant_id}:"
        if scoped_key.startswith(prefix):
            return scoped_key[len(prefix):]
        return scoped_key

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a tool is permitted for this tenant."""
        if tool_name in self.quota.denied_tools:
            return False
        if "*" in self.quota.allowed_tools:
            return True
        return tool_name in self.quota.allowed_tools

    def is_provider_allowed(self, provider_name: str) -> bool:
        """Check if a provider is permitted for this tenant."""
        if "*" in self.quota.allowed_providers:
            return True
        return provider_name in self.quota.allowed_providers

    def is_channel_allowed(self, channel_name: str) -> bool:
        """Check if a channel is permitted for this tenant."""
        if "*" in self.quota.allowed_channels:
            return True
        return channel_name in self.quota.allowed_channels

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "tier": self.tier,
            "display_name": self.display_name,
            "metadata": self.metadata,
            "quota": {
                "max_sessions": self.quota.max_sessions,
                "max_budget_usd": self.quota.max_budget_usd,
                "rate_limit_rpm": self.quota.rate_limit_rpm,
                "max_context_window_tokens": self.quota.max_context_window_tokens,
                "max_tool_iterations": self.quota.max_tool_iterations,
                "allowed_tools": self.quota.allowed_tools,
                "denied_tools": self.quota.denied_tools,
                "allowed_providers": self.quota.allowed_providers,
                "allowed_channels": self.quota.allowed_channels,
            },
        }


_DEFAULT_TENANT = TenantContext(tenant_id="default", tier="free")


_tenant_var: contextvars.ContextVar[TenantContext] = contextvars.ContextVar(
    "tenant_context", default=_DEFAULT_TENANT
)


def get_tenant() -> TenantContext:
    """Get the current tenant context."""
    return _tenant_var.get()


def set_tenant(ctx: TenantContext) -> contextvars.Token[TenantContext]:
    """Set the current tenant context. Returns a token for reset."""
    return _tenant_var.set(ctx)


def reset_tenant(token: contextvars.Token[TenantContext]) -> None:
    """Reset the tenant context to a previous value."""
    _tenant_var.reset(token)


class TenantContextManager:
    """Context manager for scoped tenant context."""

    def __init__(self, ctx: TenantContext) -> None:
        self._ctx = ctx
        self._token: contextvars.Token[TenantContext] | None = None

    def __enter__(self) -> TenantContext:
        self._token = set_tenant(self._ctx)
        return self._ctx

    def __exit__(self, *args: Any) -> None:
        if self._token is not None:
            reset_tenant(self._token)


class TenantRegistry:
    """Registry of known tenants with their configurations.

    In production, this would be backed by a database or config file.
    Here we provide an in-memory implementation with file-based loading.
    """

    def __init__(self) -> None:
        self._tenants: dict[str, TenantContext] = {
            "default": _DEFAULT_TENANT,
        }

    def register(self, ctx: TenantContext) -> None:
        self._tenants[ctx.tenant_id] = ctx
        logger.debug("[TenantRegistry] Registered tenant '{}'", ctx.tenant_id)

    def get(self, tenant_id: str) -> TenantContext | None:
        return self._tenants.get(tenant_id)

    def get_or_default(self, tenant_id: str) -> TenantContext:
        return self._tenants.get(tenant_id, _DEFAULT_TENANT)

    def remove(self, tenant_id: str) -> bool:
        if tenant_id in self._tenants and tenant_id != "default":
            del self._tenants[tenant_id]
            return True
        return False

    def list_tenants(self) -> list[str]:
        return list(self._tenants.keys())

    def load_from_config(self, data: dict[str, Any]) -> None:
        """Load tenant configurations from a dict (e.g. parsed JSON)."""
        for tenant_id, cfg in data.items():
            quota_data = cfg.get("quota", {})
            quota = TenantQuota(
                max_sessions=quota_data.get("max_sessions", 100),
                max_budget_usd=quota_data.get("max_budget_usd"),
                rate_limit_rpm=quota_data.get("rate_limit_rpm", 60),
                max_context_window_tokens=quota_data.get("max_context_window_tokens", 65_536),
                max_tool_iterations=quota_data.get("max_tool_iterations", 40),
                allowed_tools=quota_data.get("allowed_tools", ["*"]),
                denied_tools=quota_data.get("denied_tools", []),
                allowed_providers=quota_data.get("allowed_providers", ["*"]),
                allowed_channels=quota_data.get("allowed_channels", ["*"]),
            )
            self.register(TenantContext(
                tenant_id=tenant_id,
                tier=cfg.get("tier", "free"),
                display_name=cfg.get("display_name", tenant_id),
                metadata=cfg.get("metadata", {}),
                quota=quota,
            ))


_global_registry: TenantRegistry | None = None


def get_tenant_registry() -> TenantRegistry:
    """Get the global TenantRegistry (lazy-initialized)."""
    global _global_registry
    if _global_registry is None:
        _global_registry = TenantRegistry()
    return _global_registry
