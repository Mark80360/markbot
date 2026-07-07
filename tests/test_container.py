"""Tests for markbot.agent.container — AgentContext builder pattern."""


from markbot.agent.container import AgentContext


class TestAgentContextBuilder:
    def test_builder_returns_builder_instance(self):
        builder = AgentContext.builder()
        assert isinstance(builder, AgentContext.Builder)

    def test_builder_with_model(self):
        ctx = AgentContext.builder().with_model("gpt-4").build()
        assert ctx.model == "gpt-4"

    def test_builder_with_workspace(self, tmp_path):
        ctx = AgentContext.builder().with_workspace(tmp_path).build()
        assert ctx.workspace == tmp_path

    def test_builder_with_max_iterations(self):
        ctx = AgentContext.builder().with_max_iterations(10).build()
        assert ctx.max_iterations == 10

    def test_builder_with_context_window_tokens(self):
        ctx = AgentContext.builder().with_context_window_tokens(128000).build()
        assert ctx.context_window_tokens == 128000

    def test_builder_with_restrict_to_workspace(self):
        ctx = AgentContext.builder().with_restrict_to_workspace(True).build()
        assert ctx.restrict_to_workspace is True

    def test_builder_with_timezone(self):
        ctx = AgentContext.builder().with_timezone("Asia/Shanghai").build()
        assert ctx.timezone == "Asia/Shanghai"

    def test_builder_with_web_proxy(self):
        ctx = AgentContext.builder().with_web_proxy("http://proxy:8080").build()
        assert ctx.web_proxy == "http://proxy:8080"

    def test_builder_with_mcp_servers(self):
        servers = {"test": {"command": "node", "args": ["server.js"]}}
        ctx = AgentContext.builder().with_mcp_servers(servers).build()
        assert ctx.mcp_servers == servers

    def test_builder_chaining(self, tmp_path):
        ctx = (
            AgentContext.builder()
            .with_model("claude-3")
            .with_workspace(tmp_path)
            .with_max_iterations(20)
            .with_context_window_tokens(200000)
            .with_timezone("UTC")
            .build()
        )
        assert ctx.model == "claude-3"
        assert ctx.workspace == tmp_path
        assert ctx.max_iterations == 20
        assert ctx.context_window_tokens == 200000
        assert ctx.timezone == "UTC"

    def test_builder_with_budget(self):
        ctx = (
            AgentContext.builder()
            .with_budget(max_usd=10.0, warn_usd=2.0)
            .build()
        )
        assert ctx.max_budget_usd == 10.0
        assert ctx.warn_threshold_usd == 2.0

    def test_builder_defaults(self):
        ctx = AgentContext.builder().build()
        assert ctx.model == "unknown"
        assert ctx.max_iterations == 40
        assert ctx.context_window_tokens == 65_536
        assert ctx.restrict_to_workspace is False
        assert ctx.warn_threshold_usd == 0.5
        assert ctx.config is None
        assert ctx.workspace is None
        assert ctx.bus is None


class TestAgentContextDataclass:
    def test_default_values(self):
        ctx = AgentContext()
        assert ctx.model == "unknown"
        assert ctx.max_iterations == 40
        assert ctx.config is None
        assert ctx.tools is None

    def test_record_timing(self):
        ctx = AgentContext()
        ctx.record_timing("test_component", 1.5)
        assert ctx._init_timings["test_component"] == 1.5

    def test_init_summary_no_data(self):
        ctx = AgentContext()
        assert ctx.init_summary == "No timing data"

    def test_init_summary_with_data(self):
        ctx = AgentContext()
        ctx.record_timing("a", 0.1)
        ctx.record_timing("b", 0.2)
        summary = ctx.init_summary
        assert "a: 0.100s" in summary
        assert "b: 0.200s" in summary
        assert "TOTAL:" in summary


class TestProvidesProtocols:
    def test_provides_tools_protocol(self):
        from markbot.agent.container import ProvidesTools
        from markbot.tools.registry import ToolRegistry

        class FakeProvider:
            def __init__(self):
                self.tools = ToolRegistry()

        provider = FakeProvider()
        assert isinstance(provider, ProvidesTools)

    def test_provides_skills_protocol(self):
        from markbot.agent.container import ProvidesSkills

        class FakeProvider:
            def __init__(self):
                self.skill_registry = None

        provider = FakeProvider()
        assert isinstance(provider, ProvidesSkills)

    def test_provides_memory_protocol(self):
        from markbot.agent.container import ProvidesMemory

        class FakeProvider:
            def __init__(self):
                self.memory_manager = None

        provider = FakeProvider()
        assert isinstance(provider, ProvidesMemory)

    def test_provides_tools_negative(self):
        from markbot.agent.container import ProvidesTools

        class NoTools:
            pass

        assert not isinstance(NoTools(), ProvidesTools)


class TestSessionExtensionsPermissionMode:
    """``_build_session_extensions`` must apply
    ``config.agents.defaults.default_permission_mode`` to the
    ``AppStateProvider`` singleton at startup so the configured mode
    survives restarts (``/mode`` runtime overrides still work on top).

    Regression for logs/2026-07-05.log: ``/mode auto`` set in the evening
    was lost after gateway restart, leaving interactive turns in DEFAULT.
    """

    @staticmethod
    def _reset_app_state_singleton():
        from markbot.session.app_state import AppStateProvider as _ASP
        _ASP._instance = None

    def _build(self, tmp_path, default_permission_mode):
        from markbot.agent.container import AgentContext
        from markbot.config.schema import Config
        from unittest.mock import MagicMock

        config = Config.model_validate({
            "agents": {"defaults": {"default_permission_mode": default_permission_mode}}
        })
        memory_manager = MagicMock()
        memory_manager._memory_store = None
        mcp = MagicMock()
        tools = MagicMock()
        timings: dict[str, float] = {}
        self._reset_app_state_singleton()
        return AgentContext._build_session_extensions(
            tmp_path, memory_manager, mcp, tools, timings, config,
        )

    def test_config_auto_applied_to_app_state(self, tmp_path):
        from markbot.types.permission import PermissionMode
        ext = self._build(tmp_path, "auto")
        assert ext["app_state"].get().permission_mode is PermissionMode.AUTO

    def test_config_accept_edits_applied_to_app_state(self, tmp_path):
        from markbot.types.permission import PermissionMode
        ext = self._build(tmp_path, "accept_edits")
        assert ext["app_state"].get().permission_mode is PermissionMode.ACCEPT_EDITS

    def test_config_default_keeps_default(self, tmp_path):
        from markbot.types.permission import PermissionMode
        ext = self._build(tmp_path, "default")
        assert ext["app_state"].get().permission_mode is PermissionMode.DEFAULT

    def test_no_config_keeps_default(self, tmp_path):
        from markbot.types.permission import PermissionMode
        from markbot.agent.container import AgentContext
        from unittest.mock import MagicMock
        self._reset_app_state_singleton()
        ext = AgentContext._build_session_extensions(
            tmp_path, MagicMock(), MagicMock(), MagicMock(), {}, None,
        )
        assert ext["app_state"].get().permission_mode is PermissionMode.DEFAULT

    def teardown_method(self):
        self._reset_app_state_singleton()
