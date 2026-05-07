"""Tests for markbot.utils.tenancy — multi-tenancy support."""

from markbot.utils.tenancy import (
    TenantContext,
    TenantContextManager,
    TenantQuota,
    TenantRegistry,
    get_tenant,
    set_tenant,
    reset_tenant,
)


class TestTenantContext:
    def test_scoped_key(self):
        ctx = TenantContext(tenant_id="acme")
        assert ctx.scoped_key("cli:direct") == "acme:cli:direct"

    def test_unscoped_key(self):
        ctx = TenantContext(tenant_id="acme")
        assert ctx.unscoped_key("acme:cli:direct") == "cli:direct"

    def test_unscoped_key_no_prefix(self):
        ctx = TenantContext(tenant_id="acme")
        assert ctx.unscoped_key("cli:direct") == "cli:direct"

    def test_display_name_defaults_to_id(self):
        ctx = TenantContext(tenant_id="acme")
        assert ctx.display_name == "acme"

    def test_display_name_override(self):
        ctx = TenantContext(tenant_id="acme", display_name="Acme Corp")
        assert ctx.display_name == "Acme Corp"

    def test_tool_allowed_wildcard(self):
        ctx = TenantContext(tenant_id="acme")
        assert ctx.is_tool_allowed("shell") is True

    def test_tool_allowed_explicit(self):
        ctx = TenantContext(
            tenant_id="acme",
            quota=TenantQuota(allowed_tools=["read_file", "write_file"]),
        )
        assert ctx.is_tool_allowed("read_file") is True
        assert ctx.is_tool_allowed("shell") is False

    def test_tool_denied(self):
        ctx = TenantContext(
            tenant_id="acme",
            quota=TenantQuota(denied_tools=["shell"]),
        )
        assert ctx.is_tool_allowed("shell") is False
        assert ctx.is_tool_allowed("read_file") is True

    def test_provider_allowed(self):
        ctx = TenantContext(tenant_id="acme")
        assert ctx.is_provider_allowed("anthropic") is True

    def test_provider_restricted(self):
        ctx = TenantContext(
            tenant_id="acme",
            quota=TenantQuota(allowed_providers=["anthropic"]),
        )
        assert ctx.is_provider_allowed("anthropic") is True
        assert ctx.is_provider_allowed("openai") is False

    def test_channel_allowed(self):
        ctx = TenantContext(tenant_id="acme")
        assert ctx.is_channel_allowed("cli") is True

    def test_to_dict(self):
        ctx = TenantContext(tenant_id="acme", tier="enterprise")
        d = ctx.to_dict()
        assert d["tenant_id"] == "acme"
        assert d["tier"] == "enterprise"
        assert "quota" in d


class TestTenantContextVar:
    def test_default_tenant(self):
        tenant = get_tenant()
        assert tenant.tenant_id == "default"

    def test_set_and_get(self):
        ctx = TenantContext(tenant_id="test-tenant")
        token = set_tenant(ctx)
        assert get_tenant().tenant_id == "test-tenant"
        reset_tenant(token)
        assert get_tenant().tenant_id == "default"


class TestTenantContextManager:
    def test_context_manager(self):
        ctx = TenantContext(tenant_id="scoped")
        with TenantContextManager(ctx):
            assert get_tenant().tenant_id == "scoped"
        assert get_tenant().tenant_id == "default"


class TestTenantRegistry:
    def test_register_and_get(self):
        registry = TenantRegistry()
        ctx = TenantContext(tenant_id="acme")
        registry.register(ctx)
        assert registry.get("acme") is ctx

    def test_get_missing(self):
        registry = TenantRegistry()
        assert registry.get("nonexistent") is None

    def test_get_or_default(self):
        registry = TenantRegistry()
        result = registry.get_or_default("nonexistent")
        assert result.tenant_id == "default"

    def test_remove(self):
        registry = TenantRegistry()
        ctx = TenantContext(tenant_id="acme")
        registry.register(ctx)
        assert registry.remove("acme") is True
        assert registry.get("acme") is None

    def test_cannot_remove_default(self):
        registry = TenantRegistry()
        assert registry.remove("default") is False

    def test_list_tenants(self):
        registry = TenantRegistry()
        registry.register(TenantContext(tenant_id="acme"))
        registry.register(TenantContext(tenant_id="globex"))
        tenants = registry.list_tenants()
        assert "default" in tenants
        assert "acme" in tenants
        assert "globex" in tenants

    def test_load_from_config(self):
        registry = TenantRegistry()
        registry.load_from_config({
            "acme": {
                "tier": "enterprise",
                "display_name": "Acme Corp",
                "quota": {
                    "max_sessions": 50,
                    "rate_limit_rpm": 120,
                    "denied_tools": ["shell"],
                },
            }
        })
        ctx = registry.get("acme")
        assert ctx is not None
        assert ctx.tier == "enterprise"
        assert ctx.quota.max_sessions == 50
        assert ctx.quota.rate_limit_rpm == 120
        assert "shell" in ctx.quota.denied_tools
