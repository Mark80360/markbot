"""Tests for markbot.types module (exceptions, permissions, tools, skills)."""

import pytest

from markbot.types.exceptions import (
    AuthenticationError,
    BudgetExceededError,
    ConfigError,
    ConfigMigrationError,
    ConfigValidationError,
    FatalError,
    InvalidParamsError,
    MarkbotError,
    ModelNotFoundError,
    PermissionDeniedError,
    PIIExposureError,
    QuotaExceededError,
    RateLimitError,
    SecurityError,
    ServiceUnavailableError,
    SessionCorruptedError,
    SessionError,
    SessionWriteError,
    SSRFError,
    TimeoutError,
    TransientError,
)
from markbot.types.permission import PermissionDecision, PermissionMode, ToolPermissionContext
from markbot.types.skill import SkillConditions, SkillConfigVar, SkillDefinition, SkillScriptDef
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter


class TestExceptionHierarchy:
    def test_markbot_error_is_base(self):
        e = MarkbotError("test")
        assert isinstance(e, Exception)
        assert str(e) == "test"
        assert e.details == {}

    def test_markbot_error_with_details(self):
        e = MarkbotError("msg", details={"key": "val"})
        assert e.details == {"key": "val"}

    def test_transient_error_inherits(self):
        e = TransientError("temp fail")
        assert isinstance(e, MarkbotError)

    def test_rate_limit_error(self):
        e = RateLimitError("too many", retry_after_s=30.0, provider="openai")
        assert isinstance(e, TransientError)
        assert e.retry_after_s == 30.0
        assert e.provider == "openai"
        assert e.details["provider"] == "openai"

    def test_rate_limit_default_message(self):
        e = RateLimitError()
        assert "Rate limit" in str(e)

    def test_timeout_error(self):
        e = TimeoutError("timed out")
        assert isinstance(e, TransientError)
        assert isinstance(e, MarkbotError)

    def test_service_unavailable_error(self):
        e = ServiceUnavailableError("503")
        assert isinstance(e, TransientError)

    def test_fatal_error_inherits(self):
        e = FatalError("permanent")
        assert isinstance(e, MarkbotError)

    def test_authentication_error(self):
        e = AuthenticationError(provider="anthropic")
        assert isinstance(e, FatalError)
        assert e.provider == "anthropic"
        assert e.details["provider"] == "anthropic"

    def test_quota_exceeded_error(self):
        e = QuotaExceededError("no money")
        assert isinstance(e, FatalError)

    def test_model_not_found_error(self):
        e = ModelNotFoundError("gpt-5")
        assert isinstance(e, FatalError)

    def test_invalid_params_error(self):
        e = InvalidParamsError("bad args")
        assert isinstance(e, FatalError)

    def test_config_error_hierarchy(self):
        e = ConfigError("bad config")
        assert isinstance(e, MarkbotError)
        assert not isinstance(e, TransientError)
        assert not isinstance(e, FatalError)

    def test_config_validation_error(self):
        e = ConfigValidationError("invalid field")
        assert isinstance(e, ConfigError)

    def test_config_migration_error(self):
        e = ConfigMigrationError("migration failed")
        assert isinstance(e, ConfigError)

    def test_session_error_hierarchy(self):
        e = SessionError("session fail")
        assert isinstance(e, MarkbotError)

    def test_session_corrupted_error(self):
        e = SessionCorruptedError("corrupt")
        assert isinstance(e, SessionError)

    def test_session_write_error(self):
        e = SessionWriteError("write fail")
        assert isinstance(e, SessionError)

    def test_budget_exceeded_error(self):
        e = BudgetExceededError(current_cost=1.5, budget=1.0)
        assert isinstance(e, MarkbotError)
        assert e.current_cost == 1.5
        assert e.budget == 1.0

    def test_permission_denied_error(self):
        e = PermissionDeniedError("no access")
        assert isinstance(e, MarkbotError)

    def test_security_error_hierarchy(self):
        e = SecurityError("threat")
        assert isinstance(e, MarkbotError)

    def test_pii_exposure_error(self):
        e = PIIExposureError("leaked")
        assert isinstance(e, SecurityError)

    def test_ssrf_error(self):
        e = SSRFError("internal url")
        assert isinstance(e, SecurityError)

    def test_catch_all_with_markbot_error(self):
        errors = [
            RateLimitError(), TimeoutError("x"), FatalError("x"),
            ConfigError("x"), SessionError("x"), BudgetExceededError(current_cost=1.0, budget=0.5),
            PermissionDeniedError("x"), SecurityError("x"),
        ]
        for e in errors:
            with pytest.raises(MarkbotError):
                raise e


class TestPermissionMode:
    def test_all_modes_exist(self):
        assert PermissionMode.DEFAULT.value == "default"
        assert PermissionMode.PLAN.value == "plan"
        assert PermissionMode.ACCEPT_EDITS.value == "accept_edits"
        assert PermissionMode.BYPASS.value == "bypass_permissions"
        assert PermissionMode.AUTO.value == "auto"

    def test_mode_count(self):
        assert len(PermissionMode) == 5


class TestPermissionDecision:
    def test_allow_decision(self):
        d = PermissionDecision(behavior="allow")
        assert d.behavior == "allow"
        assert d.reason is None
        assert d.updated_input is None

    def test_deny_decision_with_reason(self):
        d = PermissionDecision(behavior="deny", reason="not allowed")
        assert d.behavior == "deny"
        assert d.reason == "not allowed"

    def test_ask_decision(self):
        d = PermissionDecision(behavior="ask")
        assert d.behavior == "ask"

    def test_frozen(self):
        d = PermissionDecision(behavior="allow")
        with pytest.raises(AttributeError):
            d.behavior = "deny"


class TestToolPermissionContext:
    def test_defaults(self):
        ctx = ToolPermissionContext(mode=PermissionMode.DEFAULT)
        assert ctx.mode == PermissionMode.DEFAULT
        assert ctx.always_allow == set()
        assert ctx.always_deny == set()
        assert ctx.always_ask == set()
        assert ctx.is_bypass_available is False

    def test_custom_sets(self):
        ctx = ToolPermissionContext(
            mode=PermissionMode.AUTO,
            always_allow={"read_file"},
            always_deny={"rm_rf"},
            always_ask={"exec"},
            is_bypass_available=True,
        )
        assert "read_file" in ctx.always_allow
        assert "rm_rf" in ctx.always_deny
        assert ctx.is_bypass_available is True


class TestToolParameter:
    def test_required_param(self):
        p = ToolParameter(name="path", type="string", description="File path")
        assert p.required is True
        assert p.default is None
        assert p.enum is None

    def test_optional_param(self):
        p = ToolParameter(name="limit", type="integer", description="Max", required=False, default=10)
        assert p.required is False
        assert p.default == 10

    def test_enum_param(self):
        p = ToolParameter(name="mode", type="string", description="Mode", enum=["a", "b"])
        assert p.enum == ["a", "b"]


class TestToolDefinition:
    def test_basic_definition(self):
        td = ToolDefinition(
            name="read_file",
            description="Read a file",
            parameters=[
                ToolParameter(name="path", type="string", description="File path"),
            ],
        )
        assert td.name == "read_file"
        assert td.is_read_only is False
        assert td.is_destructive is False
        assert td.aliases == []

    def test_to_openai_schema(self):
        td = ToolDefinition(
            name="read_file",
            description="Read a file",
            parameters=[
                ToolParameter(name="path", type="string", description="File path"),
                ToolParameter(name="limit", type="integer", description="Max lines", required=False),
            ],
        )
        schema = td.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "read_file"
        assert "path" in schema["function"]["parameters"]["properties"]
        assert schema["function"]["parameters"]["required"] == ["path"]

    def test_to_anthropic_schema(self):
        td = ToolDefinition(
            name="read_file",
            description="Read a file",
            parameters=[
                ToolParameter(name="path", type="string", description="File path"),
            ],
        )
        schema = td.to_anthropic_schema()
        assert schema["name"] == "read_file"
        assert "input_schema" in schema
        assert schema["input_schema"]["required"] == ["path"]

    def test_enum_in_schema(self):
        td = ToolDefinition(
            name="set_mode",
            description="Set mode",
            parameters=[
                ToolParameter(name="mode", type="string", description="Mode", enum=["fast", "slow"]),
            ],
        )
        schema = td.to_openai_schema()
        assert schema["function"]["parameters"]["properties"]["mode"]["enum"] == ["fast", "slow"]


class TestToolContext:
    def test_basic_context(self):
        ctx = ToolContext(
            session_id="s1",
            workspace="/tmp/ws",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        )
        assert ctx.session_id == "s1"
        assert ctx.is_non_interactive is False
        assert ctx.channel == ""


class TestSkillTypes:
    def test_skill_config_var(self):
        v = SkillConfigVar(key="API_KEY", description="API key", default="")
        assert v.key == "API_KEY"
        assert v.default == ""

    def test_skill_conditions_defaults(self):
        c = SkillConditions()
        assert c.requires_tools == []
        assert c.fallback_for_tools == []

    def test_skill_script_def(self):
        s = SkillScriptDef(
            name="run",
            description="Run script",
            entry="main.py",
            language="python",
            parameters=[],
        )
        assert s.language == "python"
        assert s.sandbox_config is None

    def test_skill_definition(self):
        sd = SkillDefinition(
            name="weather",
            description="Get weather",
            when_to_use="When user asks about weather",
        )
        assert sd.is_builtin is False
        assert sd.is_always_active is False
        assert sd.scripts == []
        assert sd.config_vars == []
