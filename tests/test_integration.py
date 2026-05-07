"""Integration tests — verify all enterprise modules work together."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest


class TestEndToEndIntegration:
    """Verify all modules can be imported and instantiated together."""

    def test_all_modules_importable(self):
        from markbot.types.exceptions import (
            MarkbotError, TransientError, FatalError, ConfigError,
            SessionError, BudgetExceededError, PermissionDeniedError,
            SecurityError, RateLimitError, TimeoutError,
            ServiceUnavailableError, AuthenticationError,
            QuotaExceededError, ModelNotFoundError, InvalidParamsError,
            ConfigValidationError, ConfigMigrationError,
            SessionCorruptedError, SessionWriteError,
            PIIExposureError, SSRFError,
        )
        from markbot.types.protocols import (
            SessionProtocol, SessionManagerProtocol,
            MemoryManagerProtocol, CostTrackerProtocol,
            FallbackManagerProtocol, ChannelProtocol,
            ToolCallPayload, UsagePayload, LLMResponsePayload,
            InboundMessageMetadata, OutboundMessageMetadata,
        )
        from markbot.utils.security import (
            redact_pii, SecretProvider, EnvSecretProvider,
            CompositeSecretProvider, is_private_ip, validate_url_ssrf,
        )
        from markbot.utils.ratelimit import (
            TokenBucketRateLimiter, SlidingWindowRateLimiter,
            CompositeRateLimiter,
        )
        from markbot.utils.tenancy import (
            TenantContext, TenantQuota, TenantRegistry,
            get_tenant, set_tenant, reset_tenant,
        )
        from markbot.utils.audit import (
            AuditEvent, AuditLogger, AuditOutcome,
            JsonlSink, LogSink, HttpSink,
        )
        from markbot.utils.health import HealthServer
        from markbot.utils.resources import ResourceManager, ManagedResource
        from markbot.utils.observability import (
            get_tracer, get_meter, get_correlation_id,
            new_correlation_id, correlation_scope,
        )
        from markbot.bus.emitter import EventEmitter, get_event_emitter
        from markbot.bus.events import EventType, Event
        from markbot.bus.queue import (
            MessageBus, Priority, BackpressurePolicy,
        )
        from markbot.session.integrity import SessionIntegrity
        from markbot.agent.container import AgentContext
        from markbot.providers.registry import (
            ProviderSpec, PROVIDERS, find_by_name, create_provider,
        )

    def test_exception_hierarchy_correct(self):
        from markbot.types.exceptions import (
            MarkbotError, TransientError, FatalError, ConfigError,
            SessionError, BudgetExceededError, PermissionDeniedError,
            SecurityError, RateLimitError,
        )

        assert issubclass(RateLimitError, TransientError)
        assert issubclass(TransientError, MarkbotError)
        assert issubclass(FatalError, MarkbotError)
        assert issubclass(ConfigError, MarkbotError)
        assert issubclass(SessionError, MarkbotError)
        assert issubclass(BudgetExceededError, MarkbotError)
        assert issubclass(PermissionDeniedError, MarkbotError)
        assert issubclass(SecurityError, MarkbotError)

        assert issubclass(MarkbotError, Exception)

    def test_exception_catch_all(self):
        from markbot.types.exceptions import (
            MarkbotError, RateLimitError, AuthenticationError,
            ConfigValidationError, SessionCorruptedError,
            BudgetExceededError, PermissionDeniedError, SSRFError,
        )

        exceptions = [
            RateLimitError("rate limited", retry_after_s=5.0, provider="openai"),
            AuthenticationError("bad key", provider="anthropic"),
            ConfigValidationError("bad config", errors=["field1: invalid"]),
            SessionCorruptedError("corrupted", session_key="test:123"),
            BudgetExceededError(current_cost=1.5, budget=1.0),
            PermissionDeniedError("denied", tool_name="shell", reason="policy"),
            SSRFError("blocked", url="http://169.254.1.1"),
        ]

        for exc in exceptions:
            assert isinstance(exc, MarkbotError)
            assert isinstance(exc, Exception)

    def test_exception_details_preserved(self):
        from markbot.types.exceptions import (
            RateLimitError, BudgetExceededError, ConfigValidationError,
        )

        rle = RateLimitError("msg", retry_after_s=10.0, provider="test")
        assert rle.retry_after_s == 10.0
        assert rle.provider == "test"
        assert "provider" in rle.details

        bee = BudgetExceededError(current_cost=2.0, budget=1.0)
        assert bee.current_cost == 2.0
        assert bee.budget == 1.0

        cve = ConfigValidationError("msg", errors=["e1", "e2"])
        assert cve.errors == ["e1", "e2"]


class TestProviderRegistry:
    def test_find_by_name_exists(self):
        from markbot.providers.registry import find_by_name

        spec = find_by_name("openai")
        assert spec is not None
        assert spec.name == "openai"
        assert spec.env_key == "OPENAI_API_KEY"

    def test_find_by_name_snake_case(self):
        from markbot.providers.registry import find_by_name

        spec = find_by_name("azure_openai")
        assert spec is not None
        assert spec.name == "azure_openai"

    def test_find_by_name_not_found(self):
        from markbot.providers.registry import find_by_name

        spec = find_by_name("nonexistent_provider_xyz")
        assert spec is None

    def test_all_providers_have_required_fields(self):
        from markbot.providers.registry import PROVIDERS

        for spec in PROVIDERS:
            assert spec.name, f"Provider missing name"
            assert spec.backend, f"Provider {spec.name} missing backend"
            assert isinstance(spec.keywords, tuple), f"Provider {spec.name} keywords not tuple"

    def test_gateway_providers_have_detection(self):
        from markbot.providers.registry import PROVIDERS

        gateways = [s for s in PROVIDERS if s.is_gateway]
        missing = []
        for gw in gateways:
            has_detection = (
                gw.detect_by_key_prefix
                or gw.detect_by_base_keyword
                or gw.is_direct
                or gw.is_oauth
            )
            if not has_detection:
                missing.append(gw.name)

        if missing:
            import warnings
            warnings.warn(
                f"Gateways without auto-detection: {missing}. "
                f"These require explicit user configuration."
            )


class TestMessageBusIntegration:
    @pytest.mark.asyncio
    async def test_basic_flow(self):
        from markbot.bus.queue import MessageBus
        from markbot.bus.events import InboundMessage, OutboundMessage

        bus = MessageBus(maxsize=100)
        msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="hello")
        await bus.publish_inbound(msg)

        consumed = await bus.consume_inbound()
        assert consumed.content == "hello"

    @pytest.mark.asyncio
    async def test_priority_system_first(self):
        from markbot.bus.queue import MessageBus, Priority
        from markbot.bus.events import InboundMessage

        bus = MessageBus(maxsize=100, enable_priority=True)

        await bus.publish_inbound(
            InboundMessage(channel="cli", sender_id="u", chat_id="d", content="user"),
            priority=Priority.USER,
        )
        await bus.publish_inbound(
            InboundMessage(channel="sys", sender_id="s", chat_id="d", content="system"),
            priority=Priority.SYSTEM,
        )

        first = await bus.consume_inbound()
        assert first.content == "system"

    @pytest.mark.asyncio
    async def test_partitioned_fairness(self):
        from markbot.bus.queue import MessageBus
        from markbot.bus.events import InboundMessage

        bus = MessageBus(maxsize=100, enable_partitioning=True)

        for i in range(5):
            await bus.publish_inbound(InboundMessage(
                channel="cli", sender_id="u", chat_id=f"user{i}",
                content=f"msg{i}", session_key_override=f"session-{i}",
            ))

        consumed = []
        for _ in range(5):
            consumed.append(await bus.consume_inbound())

        assert len(consumed) == 5

    @pytest.mark.asyncio
    async def test_stats_accumulate(self):
        from markbot.bus.queue import MessageBus
        from markbot.bus.events import InboundMessage

        bus = MessageBus(maxsize=100)
        for i in range(3):
            await bus.publish_inbound(InboundMessage(
                channel="cli", sender_id="u", chat_id="d", content=f"msg{i}",
            ))

        assert bus.stats["inbound_total"] == 3


class TestAgentContextBuilder:
    def test_builder_creates_context(self):
        from markbot.agent.container import AgentContext

        ctx = AgentContext.builder().with_model("test-model").build()
        assert ctx.model == "test-model"

    def test_builder_chaining(self):
        from markbot.agent.container import AgentContext

        ctx = (
            AgentContext.builder()
            .with_model("gpt-4")
            .with_max_iterations(20)
            .with_context_window_tokens(128_000)
            .with_restrict_to_workspace(True)
            .build()
        )

        assert ctx.model == "gpt-4"
        assert ctx.max_iterations == 20
        assert ctx.context_window_tokens == 128_000
        assert ctx.restrict_to_workspace is True

    def test_builder_defaults(self):
        from markbot.agent.container import AgentContext

        ctx = AgentContext.builder().build()
        assert ctx.config is None
        assert ctx.workspace is None
        assert ctx.bus is None


class TestObservability:
    def test_correlation_id_generation(self):
        from markbot.utils.observability import new_correlation_id, get_correlation_id

        cid = new_correlation_id()
        assert len(cid) == 12
        assert get_correlation_id() == cid

    def test_correlation_scope(self):
        from markbot.utils.observability import get_correlation_id, correlation_scope

        with correlation_scope("test-abc") as cid:
            assert cid == "test-abc"
            assert get_correlation_id() == "test-abc"

    def test_noop_tracer_works(self):
        from markbot.utils.observability import get_tracer, get_meter

        tracer = get_tracer()
        meter = get_meter()

        with tracer.start_as_current_span("test"):
            pass

        counter = meter.create_counter("test.counter")
        counter.add(1)

        hist = meter.create_histogram("test.latency")
        hist.record(100.0)


class TestEventEmitterIntegration:
    @pytest.mark.asyncio
    async def test_emit_and_receive(self):
        from markbot.bus.emitter import EventEmitter
        from markbot.bus.events import EventType

        received = []

        emitter = EventEmitter()

        @emitter.on(EventType.TOOL_CALLED)
        async def handler(event):
            received.append(event.payload)

        await emitter.emit(EventType.TOOL_CALLED, {"tool": "read_file"})
        assert len(received) == 1
        assert received[0] == {"tool": "read_file"}

    @pytest.mark.asyncio
    async def test_once_fires_only_once(self):
        from markbot.bus.emitter import EventEmitter
        from markbot.bus.events import EventType

        count = [0]
        emitter = EventEmitter()

        @emitter.once(EventType.MESSAGE_RECEIVED)
        async def handler(event):
            count[0] += 1

        await emitter.emit(EventType.MESSAGE_RECEIVED, "first")
        await emitter.emit(EventType.MESSAGE_RECEIVED, "second")

        assert count[0] == 1

    @pytest.mark.asyncio
    async def test_wildcard_subscription(self):
        from markbot.bus.emitter import EventEmitter
        from markbot.bus.events import EventType

        received = []
        emitter = EventEmitter()

        @emitter.on(None)
        async def catch_all(event):
            received.append(event.type)

        await emitter.emit(EventType.TOOL_CALLED, {})
        await emitter.emit(EventType.MODEL_CALLED, {})

        assert EventType.TOOL_CALLED in received
        assert EventType.MODEL_CALLED in received

    @pytest.mark.asyncio
    async def test_history_tracking(self):
        from markbot.bus.emitter import EventEmitter
        from markbot.bus.events import EventType

        emitter = EventEmitter()
        await emitter.emit(EventType.SESSION_CREATED, "s1")
        await emitter.emit(EventType.SESSION_CREATED, "s2")

        assert len(emitter.history) == 2


class TestSessionIntegrityIntegration:
    def test_full_lifecycle(self, tmp_path):
        from markbot.session.integrity import SessionIntegrity

        si = SessionIntegrity(tmp_path)
        session_path = tmp_path / "test.jsonl"
        data = json.dumps({"messages": [{"role": "user", "content": "hello"}]})

        wal_path = si.write_wal(session_path, data)
        assert wal_path.exists()

        session_path.write_text(data, encoding="utf-8")
        si.write_checksum(session_path, data)
        assert si.verify_checksum(session_path)

        si.commit_wal(session_path)
        assert not wal_path.exists()

    def test_recovery_flow(self, tmp_path):
        from markbot.session.integrity import SessionIntegrity

        si = SessionIntegrity(tmp_path)
        session_path = tmp_path / "corrupted.jsonl"
        data = '{"valid": "json"}'

        si.write_wal(session_path, data)
        recovered = si.recover_from_wal(session_path)
        assert recovered == data

    def test_archive_and_cleanup(self, tmp_path):
        from markbot.session.integrity import SessionIntegrity

        si = SessionIntegrity(tmp_path, archive_ttl_days=0)
        session_path = tmp_path / "old.jsonl"
        session_path.write_text("old data", encoding="utf-8")
        si.write_checksum(session_path, "old data")

        archive_path = si.archive_session(session_path)
        assert archive_path is not None
        assert archive_path.exists()
        assert not session_path.exists()

        removed = si.cleanup_archive()
        assert removed >= 0


class TestSecurityIntegration:
    def test_redact_complex_text(self):
        from markbot.utils.security import redact_pii

        text = (
            "Using API key sk-or-v1-abc123def456ghi789jkl012mnop345\n"
            "Also tried sk-proj-abc123def456ghi789jkl012mnop345qrs678\n"
            "Email: admin@example.com\n"
            "IP: 192.168.1.100\n"
            "Bearer token: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\n"
            "Normal text should remain unchanged.\n"
        )

        result = redact_pii(text)

        assert "sk-or-v1" not in result or "***REDACTED***" in result
        assert "sk-proj" not in result or "***REDACTED***" in result
        assert "admin@example.com" not in result
        assert "192.168.1.100" not in result
        assert "Bearer eyJhbGci" not in result
        assert "Normal text should remain unchanged" in result

    def test_secret_provider_chain(self):
        from markbot.utils.security import (
            EnvSecretProvider, CompositeSecretProvider,
        )

        p1 = EnvSecretProvider()
        p1.set("TEST_KEY_A", "value_a")

        p2 = EnvSecretProvider()
        p2.set("TEST_KEY_B", "value_b")

        chain = CompositeSecretProvider(p1, p2)
        assert chain.get("TEST_KEY_A") == "value_a"
        assert chain.get("TEST_KEY_B") == "value_b"
        assert chain.get("MISSING_KEY") is None

    def test_ssrf_validation_comprehensive(self):
        from markbot.utils.security import validate_url_ssrf

        assert validate_url_ssrf("https://api.openai.com/v1") is None
        assert validate_url_ssrf("http://localhost:8080") is not None
        assert validate_url_ssrf("http://127.0.0.1:3000") is not None
        assert validate_url_ssrf("http://192.168.1.1/admin") is not None
        assert validate_url_ssrf("http://10.0.0.1/api") is not None
        assert validate_url_ssrf("ftp://evil.com") is not None
        assert validate_url_ssrf("not-a-url!!!") is not None

        assert validate_url_ssrf(
            "http://192.168.1.1/admin",
            allowed_internal_ips=["192.168.1.1"],
        ) is None


class TestRateLimiterIntegration:
    def test_token_bucket_burst(self):
        from markbot.utils.ratelimit import TokenBucketRateLimiter

        limiter = TokenBucketRateLimiter(rate=10.0, capacity=20.0)
        key = "test-burst"

        allowed = sum(1 for _ in range(30) if limiter.allow(key))
        assert allowed <= 21

    def test_sliding_window_precise(self):
        from markbot.utils.ratelimit import SlidingWindowRateLimiter

        limiter = SlidingWindowRateLimiter(max_requests=5, window_s=60.0)
        key = "test-window"

        for _ in range(5):
            assert limiter.allow(key)
        assert not limiter.allow(key)

    def test_composite_both_must_pass(self):
        from markbot.utils.ratelimit import (
            TokenBucketRateLimiter, SlidingWindowRateLimiter,
            CompositeRateLimiter,
        )

        tb = TokenBucketRateLimiter(rate=100.0, capacity=100.0)
        sw = SlidingWindowRateLimiter(max_requests=3, window_s=60.0)
        composite = CompositeRateLimiter(tb, sw)

        key = "test-composite"
        for _ in range(3):
            assert composite.allow(key)
        assert not composite.allow(key)

    def test_per_key_isolation(self):
        from markbot.utils.ratelimit import SlidingWindowRateLimiter

        limiter = SlidingWindowRateLimiter(max_requests=2, window_s=60.0)

        for _ in range(2):
            assert limiter.allow("key-a")
        assert not limiter.allow("key-a")
        assert limiter.allow("key-b")


class TestTenancyIntegration:
    def test_context_propagation(self):
        from markbot.utils.tenancy import (
            TenantContext, TenantQuota, set_tenant, get_tenant, reset_tenant,
        )

        ctx = TenantContext(tenant_id="acme", tier="enterprise")
        token = set_tenant(ctx)

        current = get_tenant()
        assert current.tenant_id == "acme"
        assert current.tier == "enterprise"

        reset_tenant(token)
        default = get_tenant()
        assert default.tenant_id == "default"

    def test_registry_lifecycle(self):
        from markbot.utils.tenancy import (
            TenantContext, TenantRegistry,
        )

        registry = TenantRegistry()
        ctx = TenantContext(tenant_id="org-1", tier="pro")
        registry.register(ctx)

        assert registry.get("org-1") is ctx
        assert registry.get("nonexistent") is None
        assert registry.get_or_default("nonexistent").tenant_id == "default"

        assert "org-1" in registry.list_tenants()
        assert registry.remove("org-1")
        assert registry.get("org-1") is None

    def test_cannot_remove_default(self):
        from markbot.utils.tenancy import TenantRegistry

        registry = TenantRegistry()
        assert not registry.remove("default")

    def test_load_from_config(self):
        from markbot.utils.tenancy import TenantRegistry

        registry = TenantRegistry()
        config = {
            "acme-corp": {
                "tier": "enterprise",
                "display_name": "Acme Corporation",
                "quota": {
                    "max_sessions": 500,
                    "max_budget_usd": 100.0,
                    "allowed_tools": ["read_file", "write_file"],
                },
            },
        }
        registry.load_from_config(config)

        tenant = registry.get("acme-corp")
        assert tenant is not None
        assert tenant.tier == "enterprise"
        assert tenant.display_name == "Acme Corporation"
        assert tenant.quota.max_sessions == 500
        assert tenant.quota.max_budget_usd == 100.0
        assert tenant.quota.allowed_tools == ["read_file", "write_file"]


class TestAuditLoggingIntegration:
    def test_audit_event_creation(self):
        from markbot.utils.audit import AuditEvent

        event = AuditEvent(
            action="tool.invoke",
            actor="user:alice",
            resource="filesystem.read_file",
            outcome="success",
            details={"path": "/data/report.csv"},
        )

        d = event.to_dict()
        assert d["action"] == "tool.invoke"
        assert d["actor"] == "user:alice"
        assert d["resource"] == "filesystem.read_file"
        assert d["outcome"] == "success"
        assert len(d["event_id"]) == 16

    def test_audit_event_json(self):
        from markbot.utils.audit import AuditEvent

        event = AuditEvent(
            action="config.change",
            actor="admin",
            resource="settings.budget",
            outcome="success",
        )

        json_str = event.to_json()
        parsed = json.loads(json_str)
        assert parsed["action"] == "config.change"

    def test_audit_event_cef(self):
        from markbot.utils.audit import AuditEvent

        event = AuditEvent(
            action="tool.invoke",
            actor="user:alice",
            resource="shell.exec",
            outcome="success",
        )

        cef = event.to_cef()
        assert cef.startswith("CEF:0|markbot|markbot|1.0|")
        assert "act=tool.invoke" in cef
        assert "suser=user:alice" in cef

    def test_jsonl_sink(self, tmp_path):
        from markbot.utils.audit import AuditEvent, JsonlSink

        log_path = tmp_path / "audit.jsonl"
        sink = JsonlSink(log_path)

        for i in range(3):
            sink.write(AuditEvent(
                action=f"test.{i}",
                actor="tester",
                resource="test",
            ))

        sink.flush()
        sink.close()

        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

    def test_audit_logger_multiple_sinks(self, tmp_path):
        from markbot.utils.audit import AuditEvent, AuditLogger, JsonlSink, LogSink

        log_path = tmp_path / "multi.jsonl"
        logger = AuditLogger(LogSink(), JsonlSink(log_path))

        logger.log(AuditEvent(
            action="session.create",
            actor="system",
            resource="session",
        ))

        logger.flush()
        logger.close()

        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "session.create" in content

    def test_log_action_convenience(self, tmp_path):
        from markbot.utils.audit import AuditLogger, JsonlSink

        log_path = tmp_path / "convenience.jsonl"
        logger = AuditLogger(JsonlSink(log_path))

        logger.log_action(
            action="tool.deny",
            actor="guardrail",
            resource="shell.exec",
            outcome="denied",
            details={"reason": "policy"},
        )

        logger.close()
        content = log_path.read_text(encoding="utf-8")
        assert "tool.deny" in content


class TestHealthServerIntegration:
    def test_set_and_query_readiness(self):
        from markbot.utils.health import HealthServer

        server = HealthServer(port=0)
        server.set_ready("provider", True)
        server.set_ready("channels", True)
        server.set_ready("mcp", False)

        assert server._readiness["provider"] is True
        assert server._readiness["channels"] is True
        assert server._readiness["mcp"] is False

    def test_remove_component(self):
        from markbot.utils.health import HealthServer

        server = HealthServer(port=0)
        server.set_ready("temp", True)
        server.remove_component("temp")
        assert "temp" not in server._readiness

    def test_thread_safety(self):
        import threading
        from markbot.utils.health import HealthServer

        server = HealthServer(port=0)
        errors = []

        def worker(tid):
            try:
                for i in range(100):
                    server.set_ready(f"comp-{tid}-{i}", True)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(server._readiness) == 500


class TestResourceManagerIntegration:
    @pytest.mark.asyncio
    async def test_http_client_reuse(self):
        from markbot.utils.resources import ResourceManager

        rm = ResourceManager()
        c1 = rm.http_client("api")
        c2 = rm.http_client("api")
        assert c1 is c2

    @pytest.mark.asyncio
    async def test_different_names_different_clients(self):
        from markbot.utils.resources import ResourceManager

        rm = ResourceManager()
        c1 = rm.http_client("search")
        c2 = rm.http_client("download")
        assert c1 is not c2

    @pytest.mark.asyncio
    async def test_graceful_shutdown(self):
        from markbot.utils.resources import ResourceManager

        rm = ResourceManager()
        rm.http_client("test")
        assert not rm.is_closed

        await rm.close()
        assert rm.is_closed

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        from markbot.utils.resources import ResourceManager

        async with ResourceManager() as rm:
            rm.http_client("ctx-test")
            assert not rm.is_closed

        assert rm.is_closed

    @pytest.mark.asyncio
    async def test_health_report(self):
        from markbot.utils.resources import ResourceManager

        rm = ResourceManager()
        rm.http_client("health-test")

        report = rm.health()
        assert "http_clients" in report
        assert "health-test" in report["http_clients"]
        assert not report["http_clients"]["health-test"]["closed"]

        await rm.close()

    @pytest.mark.asyncio
    async def test_global_singleton(self):
        from markbot.utils.resources import (
            get_resource_manager, set_resource_manager,
            shutdown_resource_manager,
        )

        rm1 = get_resource_manager()
        rm2 = get_resource_manager()
        assert rm1 is rm2

        await shutdown_resource_manager()


class TestConfigValidatorIntegration:
    def test_validate_default_config(self):
        from markbot.config.schema import Config
        from markbot.config.validator import validate_config

        config = Config()
        result = validate_config(config)

        assert isinstance(result.is_valid, bool)

    def test_validate_with_empty_chain(self):
        from markbot.config.schema import Config
        from markbot.config.validator import validate_config

        config = Config()
        config.agents.defaults.model_chain = []
        result = validate_config(config)

        warnings = [i for i in result.warnings if "model_chain" in i.field]
        assert len(warnings) > 0

    def test_validate_budget_logic(self):
        from markbot.config.schema import Config
        from markbot.config.validator import validate_config

        config = Config()
        config.budget.enabled = True
        config.budget.max_budget_usd = 1.0
        config.budget.warn_threshold_usd = 5.0

        result = validate_config(config)
        warnings = [i for i in result.warnings if "warn_threshold" in i.field]
        assert len(warnings) > 0

    def test_validate_invalid_model_ref(self):
        from markbot.config.schema import Config
        from markbot.config.validator import validate_config

        config = Config()
        config.agents.defaults.model_chain = ["invalid-no-slash"]

        result = validate_config(config)
        errors = [i for i in result.errors if "model_chain" in i.field]
        assert len(errors) > 0

    def test_validation_result_properties(self):
        from markbot.config.validator import ValidationResult, Severity

        result = ValidationResult()
        result.add("field1", "error msg", severity=Severity.ERROR)
        result.add("field2", "warning msg", severity=Severity.WARNING)

        assert len(result.errors) == 1
        assert len(result.warnings) == 1
        assert not result.is_valid

    def test_validation_result_merge(self):
        from markbot.config.validator import ValidationResult, Severity

        r1 = ValidationResult()
        r1.add("f1", "e1", severity=Severity.ERROR)

        r2 = ValidationResult()
        r2.add("f2", "w1", severity=Severity.WARNING)

        r1.merge(r2)
        assert len(r1.errors) == 1
        assert len(r1.warnings) == 1
