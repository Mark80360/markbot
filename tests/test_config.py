"""Tests for markbot.config module (schema, validator)."""


from markbot.config.schema import (
    AgentDefaults,
    ChannelsConfig,
    CodeExecutionConfig,
    CompactionConfig,
    ExecToolConfig,
    FilesystemToolConfig,
    GatewayConfig,
    MCPServerConfig,
    MemoryToolsConfig,
    ModelConfig,
    ProviderConfig,
    ProvidersConfig,
    ToolsConfig,
    WebToolsConfig,
)
from markbot.config.validator import (
    Severity,
    ValidationIssue,
    ValidationResult,
)


class TestAgentDefaults:
    def test_defaults(self):
        ad = AgentDefaults()
        assert ad.max_tokens == 8192
        assert ad.context_window_tokens == 65_536
        assert ad.temperature == 0.1
        assert ad.max_tool_iterations == 40
        assert ad.timezone == "UTC"

    def test_custom_values(self):
        ad = AgentDefaults(max_tokens=4096, temperature=0.5)
        assert ad.max_tokens == 4096
        assert ad.temperature == 0.5

    def test_timezone_normalization(self):
        ad = AgentDefaults(timezone="UTC+8")
        assert ad.timezone == "Asia/Shanghai"


class TestModelConfig:
    def test_required_fields(self):
        mc = ModelConfig(id="gpt4", name="gpt-4")
        assert mc.id == "gpt4"
        assert mc.name == "gpt-4"
        assert mc.max_tokens == 8192

    def test_custom_values(self):
        mc = ModelConfig(
            id="sonnet", name="claude-sonnet-4-5",
            max_tokens=16384, context_window=200000,
            temperature=0.3,
        )
        assert mc.max_tokens == 16384
        assert mc.context_window == 200000


class TestProviderConfig:
    def test_defaults(self):
        pc = ProviderConfig()
        assert pc.api_key == ""
        assert pc.models == []
        assert pc.is_configured is False

    def test_configured_provider(self):
        pc = ProviderConfig(
            api_key="sk-test",
            models=[ModelConfig(id="m1", name="model-1")],
        )
        assert pc.is_configured is True

    def test_get_model(self):
        pc = ProviderConfig(
            api_key="sk-test",
            models=[
                ModelConfig(id="m1", name="model-1"),
                ModelConfig(id="m2", name="model-2"),
            ],
        )
        assert pc.get_model("m1").name == "model-1"
        assert pc.get_model("m2").name == "model-2"
        assert pc.get_model("nonexistent") is None


class TestProvidersConfig:
    def test_defaults(self):
        pc = ProvidersConfig()
        assert pc.anthropic.api_key == ""
        assert pc.openai.api_key == ""

    def test_get_provider(self):
        pc = ProvidersConfig()
        assert pc.get_provider("anthropic") is not None
        assert pc.get_provider("nonexistent") is None

    def test_set_provider_builtin(self):
        pc = ProvidersConfig()
        new_config = ProviderConfig(api_key="sk-new")
        pc.set_provider("anthropic", new_config)
        assert pc.get_provider("anthropic").api_key == "sk-new"

    def test_set_provider_dynamic(self):
        pc = ProvidersConfig()
        custom = ProviderConfig(api_key="sk-custom")
        pc.set_provider("my_provider", custom)
        assert pc.get_provider("my_provider") is not None
        assert pc.get_provider("my_provider").api_key == "sk-custom"

    def test_list_provider_ids_empty(self):
        pc = ProvidersConfig()
        ids = pc.list_provider_ids()
        assert ids == []

    def test_list_provider_ids_configured(self):
        pc = ProvidersConfig(
            anthropic=ProviderConfig(
                api_key="sk-ant",
                models=[ModelConfig(id="m1", name="claude")],
            )
        )
        ids = pc.list_provider_ids()
        assert "anthropic" in ids


class TestChannelsConfig:
    def test_defaults(self):
        cc = ChannelsConfig()
        assert cc.send_progress is True
        assert cc.send_tool_hints is False
        assert cc.send_max_retries == 3

    def test_extra_fields(self):
        cc = ChannelsConfig(dingtalk={"token": "abc"})
        assert hasattr(cc, "dingtalk")


class TestGatewayConfig:
    def test_defaults(self):
        gc = GatewayConfig()
        assert gc.host == "0.0.0.0"
        assert gc.port == 18790
        assert gc.heartbeat.enabled is True
        assert gc.heartbeat.interval_s == 1800


class TestWebToolsConfig:
    def test_defaults(self):
        wtc = WebToolsConfig()
        assert wtc.proxy is None
        assert wtc.search.provider == "brave"
        assert wtc.search.max_results == 5


class TestExecToolConfig:
    def test_defaults(self):
        etc = ExecToolConfig()
        assert etc.enable is True
        assert etc.timeout == 60
        assert etc.allowed_internal_ips == []


class TestFilesystemToolConfig:
    def test_defaults(self):
        ftc = FilesystemToolConfig()
        assert ftc.safe_delete is True
        assert ftc.max_backups == 50


class TestCodeExecutionConfig:
    def test_defaults(self):
        cec = CodeExecutionConfig()
        assert cec.enable is True
        assert cec.timeout == 60
        assert cec.max_memory_mb == 256


class TestMemoryToolsConfig:
    def test_defaults(self):
        mtc = MemoryToolsConfig()
        assert mtc.embedding_backend == "openai"
        assert mtc.memory_summary_enabled is True
        assert mtc.context_compact_enabled is True


class TestMCPServerConfig:
    def test_defaults(self):
        mc = MCPServerConfig()
        assert mc.command == ""
        assert mc.url == ""
        assert mc.tool_timeout == 30
        assert mc.enabled_tools == ["*"]


class TestToolsConfig:
    def test_defaults(self):
        tc = ToolsConfig()
        assert tc.restrict_to_workspace is False
        assert tc.mcp_servers == {}


class TestCompactionConfig:
    def test_defaults(self):
        cc = CompactionConfig()
        assert cc.collapse_tool_result_chars == 4000
        assert cc.micro_compact_keep_turns == 6
        assert cc.threshold_ratio == 0.85


class TestValidationResult:
    def test_empty_is_valid(self):
        vr = ValidationResult()
        assert vr.is_valid is True
        assert vr.errors == []
        assert vr.warnings == []

    def test_add_error(self):
        vr = ValidationResult()
        vr.add("field", "bad value")
        assert vr.is_valid is False
        assert len(vr.errors) == 1

    def test_add_warning(self):
        vr = ValidationResult()
        vr.add("field", "suspicious", severity=Severity.WARNING)
        assert vr.is_valid is True
        assert len(vr.warnings) == 1

    def test_merge(self):
        vr1 = ValidationResult()
        vr1.add("f1", "error1")
        vr2 = ValidationResult()
        vr2.add("f2", "error2", severity=Severity.WARNING)
        vr1.merge(vr2)
        assert len(vr1.issues) == 2


class TestValidationIssue:
    def test_basic_issue(self):
        vi = ValidationIssue(field="test", message="bad")
        assert vi.field == "test"
        assert vi.severity == Severity.ERROR
        assert vi.suggestion == ""

    def test_issue_with_suggestion(self):
        vi = ValidationIssue(
            field="api_key",
            message="missing",
            suggestion="Set ANTHROPIC_API_KEY env var",
        )
        assert vi.suggestion == "Set ANTHROPIC_API_KEY env var"

class TestProvidersConfigIsolation:
    def test_dynamic_providers_not_shared_between_instances(self):
        """Each ProvidersConfig instance must have its own _dynamic_providers dict."""
        pc1 = ProvidersConfig()
        pc2 = ProvidersConfig()
        custom = ProviderConfig(api_key="sk-custom")
        pc1.set_provider("my_custom", custom)
        assert pc1.get_provider("my_custom") is not None
        assert pc2.get_provider("my_custom") is None
