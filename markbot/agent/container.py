"""Agent dependency-injection container.

Encapsulates all the components that ``AgentLoop`` needs, replacing
the 20+ parameter ``__init__`` with a clean builder pattern.

Usage::

    ctx = (AgentContext.builder()
        .with_config(config)
        .with_workspace(workspace)
        .with_bus(bus)
        .build())

    loop = AgentLoop(ctx)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loguru import logger

if TYPE_CHECKING:
    from markbot.agent.compact import CompactionConfig, MultiLevelCompactor
    from markbot.agent.context import ContextBuilder
    from markbot.agent.cost import CostTracker
    from markbot.agent.hooks.bootstrap import BootstrapHook
    from markbot.agent.hooks.compaction import MemoryCompactionHook
    from markbot.agent.mcp.manager import McpManager
    from markbot.agent.pipeline.engine import MessagePipeline
    from markbot.agent.services.executor import ToolExecutor
    from markbot.agent.services.interaction import InteractionLogger
    from markbot.agent.subagent import SubagentManager
    from markbot.bus.queue import MessageBus
    from markbot.cli.slash_commands import CommandRouter
    from markbot.config.schema import (
        BudgetConfig,
        ChannelsConfig,
        CodeExecutionConfig,
        Config,
        ExecToolConfig,
        FilesystemToolConfig,
        MemoryToolsConfig,
        WebSearchConfig,
    )
    from markbot.memory.base import BaseMemoryManager
    from markbot.memory.daily_log import DailyLogManager
    from markbot.memory.encoder import MemoryEncoder
    from markbot.providers.fallback import FallbackManager
    from markbot.schedule.cron import CronService
    from markbot.session.app_state import AppStateProvider
    from markbot.session.bootstrap import SessionBootstrap
    from markbot.session.handoff import HandoffManager
    from markbot.session.session import SessionManager
    from markbot.session.task_tracker import TaskTracker
    from markbot.skills import SkillRegistry
    from markbot.skills.core.guardrail import SkillGuardrailManager
    from markbot.tools.memory import MemorySearchTool
    from markbot.tools.message import MessageTool
    from markbot.tools.question import AskUserQuestionTool
    from markbot.tools.registry import ToolRegistry


# Cache layer (runtime imports — these are instantiated below in
# from_legacy_params, so they MUST be importable at module load time,
# not only under TYPE_CHECKING).
from markbot.agent.prefix_cache import PrefixStabilityManager
from markbot.agent.llm_response_cache import LLMResponseCache
from markbot.agent.token_estimate_cache import TokenEstimateCache


@runtime_checkable
class ProvidesTools(Protocol):
    tools: "ToolRegistry"


@runtime_checkable
class ProvidesSkills(Protocol):
    skill_registry: "SkillRegistry"


@runtime_checkable
class ProvidesMemory(Protocol):
    memory_manager: "BaseMemoryManager"


@dataclass
class AgentContext:
    """Immutable container for all agent dependencies.

    Built via ``AgentContext.builder()`` 鈥?never instantiate directly.
    """

    config: "Config | None" = None
    workspace: Path | None = None
    bus: "MessageBus | None" = None
    fallback_manager: "FallbackManager | None" = None
    model: str = "unknown"
    max_iterations: int = 40
    context_window_tokens: int = 65_536
    channels_config: "ChannelsConfig | None" = None
    web_search_config: "WebSearchConfig | None" = None
    web_proxy: str | None = None
    exec_config: "ExecToolConfig | None" = None
    filesystem_config: "FilesystemToolConfig | None" = None
    memory_config: "MemoryToolsConfig | None" = None
    code_execution_config: "CodeExecutionConfig | None" = None
    cron_service: "CronService | None" = None
    restrict_to_workspace: bool = False
    timezone: str | None = None
    compaction_config: "CompactionConfig | None" = None
    max_budget_usd: float | None = None
    warn_threshold_usd: float = 0.5
    budget_config: "BudgetConfig | None" = None
    mcp_servers: dict | None = None

    tools: "ToolRegistry | None" = None
    skill_registry: "SkillRegistry | None" = None
    guardrail_manager: "SkillGuardrailManager | None" = None
    sessions: "SessionManager | None" = None
    subagents: "SubagentManager | None" = None
    memory_manager: "BaseMemoryManager | None" = None
    compactor: "MultiLevelCompactor | None" = None
    cost_tracker: "CostTracker | None" = None
    mcp: "McpManager | None" = None
    context_builder: "ContextBuilder | None" = None
    pipeline: "MessagePipeline | None" = None
    commands: "CommandRouter | None" = None
    tool_executor: "ToolExecutor | None" = None
    bootstrap_hook: "BootstrapHook | None" = None
    compaction_hook: "MemoryCompactionHook | None" = None
    daily_log: "DailyLogManager | None" = None
    interaction_log: "InteractionLogger | None" = None
    memory_search_tool: "MemorySearchTool | None" = None
    question_tool: "AskUserQuestionTool | None" = None
    message_tool: "MessageTool | None" = None
    handoff_manager: "HandoffManager | None" = None
    session_bootstrap: "SessionBootstrap | None" = None
    task_tracker: "TaskTracker | None" = None
    memory_encoder: "MemoryEncoder | None" = None
    app_state: "AppStateProvider | None" = None

    # ------------------------------------------------------------------
    # Cache layer (see markbot.agent.{prefix_cache,llm_response_cache,
    # token_estimate_cache,prompt_persist}).
    # ------------------------------------------------------------------
    prefix_stability: Any = None
    llm_response_cache: Any = None
    token_estimate_cache: Any = None

    _init_timings: dict[str, float] = field(default_factory=dict)

    class Builder:
        """Fluent builder for AgentContext."""

        def __init__(self) -> None:
            self._params: dict[str, Any] = {}

        def with_config(self, config: "Config") -> "AgentContext.Builder":
            self._params["config"] = config
            return self

        def with_workspace(self, workspace: Path) -> "AgentContext.Builder":
            self._params["workspace"] = workspace
            return self

        def with_bus(self, bus: "MessageBus") -> "AgentContext.Builder":
            self._params["bus"] = bus
            return self

        def with_fallback_manager(self, fm: "FallbackManager") -> "AgentContext.Builder":
            self._params["fallback_manager"] = fm
            return self

        def with_model(self, model: str) -> "AgentContext.Builder":
            self._params["model"] = model
            return self

        def with_max_iterations(self, n: int) -> "AgentContext.Builder":
            self._params["max_iterations"] = n
            return self

        def with_context_window_tokens(self, n: int) -> "AgentContext.Builder":
            self._params["context_window_tokens"] = n
            return self

        def with_channels_config(self, cfg: "ChannelsConfig") -> "AgentContext.Builder":
            self._params["channels_config"] = cfg
            return self

        def with_web_search_config(self, cfg: "WebSearchConfig") -> "AgentContext.Builder":
            self._params["web_search_config"] = cfg
            return self

        def with_web_proxy(self, proxy: str | None) -> "AgentContext.Builder":
            self._params["web_proxy"] = proxy
            return self

        def with_exec_config(self, cfg: "ExecToolConfig") -> "AgentContext.Builder":
            self._params["exec_config"] = cfg
            return self

        def with_filesystem_config(self, cfg: "FilesystemToolConfig") -> "AgentContext.Builder":
            self._params["filesystem_config"] = cfg
            return self

        def with_memory_config(self, cfg: "MemoryToolsConfig") -> "AgentContext.Builder":
            self._params["memory_config"] = cfg
            return self

        def with_code_execution_config(self, cfg: "CodeExecutionConfig") -> "AgentContext.Builder":
            self._params["code_execution_config"] = cfg
            return self

        def with_cron_service(self, svc: "CronService") -> "AgentContext.Builder":
            self._params["cron_service"] = svc
            return self

        def with_restrict_to_workspace(self, v: bool) -> "AgentContext.Builder":
            self._params["restrict_to_workspace"] = v
            return self

        def with_timezone(self, tz: str | None) -> "AgentContext.Builder":
            self._params["timezone"] = tz
            return self

        def with_compaction_config(self, cfg: "CompactionConfig") -> "AgentContext.Builder":
            self._params["compaction_config"] = cfg
            return self

        def with_budget(self, max_usd: float | None, warn_usd: float = 0.5, budget_config: "BudgetConfig | None" = None) -> "AgentContext.Builder":
            self._params["max_budget_usd"] = max_usd
            self._params["warn_threshold_usd"] = warn_usd
            self._params["budget_config"] = budget_config
            return self

        def with_mcp_servers(self, servers: dict | None) -> "AgentContext.Builder":
            self._params["mcp_servers"] = servers
            return self

        def with_tools(self, tools: "ToolRegistry") -> "AgentContext.Builder":
            self._params["tools"] = tools
            return self

        def with_skill_registry(self, sr: "SkillRegistry") -> "AgentContext.Builder":
            self._params["skill_registry"] = sr
            return self

        def with_sessions(self, sm: "SessionManager") -> "AgentContext.Builder":
            self._params["sessions"] = sm
            return self

        def with_subagents(self, sa: "SubagentManager") -> "AgentContext.Builder":
            self._params["subagents"] = sa
            return self

        def with_memory_manager(self, mm: "BaseMemoryManager") -> "AgentContext.Builder":
            self._params["memory_manager"] = mm
            return self

        def with_compactor(self, c: "MultiLevelCompactor") -> "AgentContext.Builder":
            self._params["compactor"] = c
            return self

        def with_cost_tracker(self, ct: "CostTracker") -> "AgentContext.Builder":
            self._params["cost_tracker"] = ct
            return self

        def with_mcp(self, mcp: "McpManager") -> "AgentContext.Builder":
            self._params["mcp"] = mcp
            return self

        def with_context_builder(self, cb: "ContextBuilder") -> "AgentContext.Builder":
            self._params["context_builder"] = cb
            return self

        def with_pipeline(self, p: "MessagePipeline") -> "AgentContext.Builder":
            self._params["pipeline"] = p
            return self

        def with_commands(self, cr: "CommandRouter") -> "AgentContext.Builder":
            self._params["commands"] = cr
            return self

        def with_tool_executor(self, te: "ToolExecutor") -> "AgentContext.Builder":
            self._params["tool_executor"] = te
            return self

        def build(self) -> "AgentContext":
            ctx = AgentContext(**self._params)
            return ctx

    @classmethod
    def builder(cls) -> Builder:
        return cls.Builder()

    @classmethod
    def from_legacy_params(
        cls,
        bus: "MessageBus",
        fallback_manager: "FallbackManager",
        config: "Config | None" = None,
        workspace: Path | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        web_search_config: "WebSearchConfig | None" = None,
        web_proxy: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        filesystem_config: "FilesystemToolConfig | None" = None,
        memory_config: "MemoryToolsConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: "SessionManager | None" = None,
        mcp_servers: dict | None = None,
        channels_config: "ChannelsConfig | None" = None,
        timezone: str | None = None,
        compaction_config: "CompactionConfig | None" = None,
        max_budget_usd: float | None = None,
        warn_threshold_usd: float = 0.5,
        budget_config: "BudgetConfig | None" = None,
    ) -> "AgentContext":
        """Create a fully initialized AgentContext from the legacy parameter list.

        This encapsulates all the component creation that was previously
        inlined in ``AgentLoop.__init__``.  The body delegates to a series
        of ``_build_*`` sub-builders so each concern is isolated and
        individually testable.
        """
        _init_start = time.time()
        logger.info("Starting initialization...")
        timings: dict[str, float] = {}

        model, primary_provider_config = cls._resolve_model(config)
        resolved_configs = cls._resolve_tool_configs(
            web_search_config, exec_config, filesystem_config, memory_config, config
        )
        resolved_configs["web_proxy"] = web_proxy
        compactor, cost_tracker = cls._build_cost_and_compaction(
            fallback_manager, compaction_config, budget_config,
            max_budget_usd, warn_threshold_usd, timings,
        )
        tools, skill_registry, guardrail_manager = cls._build_tools_and_skills(
            workspace, timings,
        )
        sessions, subagents, mcp = cls._build_sessions_subagents_mcp(
            fallback_manager, config, workspace, bus, model,
            resolved_configs, restrict_to_workspace, cost_tracker,
            session_manager, mcp_servers, timings,
        )
        memory_manager = cls._build_memory(
            workspace, fallback_manager, model, primary_provider_config,
            resolved_configs, timezone, timings,
        )
        subagents._memory_manager = memory_manager
        bootstrap_hook, compaction_hook, context_builder = cls._build_hooks_and_context(
            workspace, memory_manager, resolved_configs["memory"],
            compaction_config, context_window_tokens,
            timezone, tools, skill_registry,
        )
        binder = cls._build_tool_binder(
            tools, workspace, restrict_to_workspace, resolved_configs, config,
            cron_service, subagents, memory_manager, skill_registry, timezone, bus, timings,
        )
        logger.info("Agent has {} tools available", len(tools))
        pipeline, daily_log, interaction_log = cls._build_pipeline(
            binder, memory_manager, sessions, workspace,
        )
        session_ext = cls._build_session_extensions(
            workspace, memory_manager, mcp, tools, timings, config,
        )

        timings["total"] = time.time() - _init_start
        logger.info("Initialization complete, total took {:.3f}s", timings["total"])
        logger.info("Timings breakdown:\n{}", "\n".join(f"  {k}: {v:.3f}s" for k, v in sorted(timings.items())))

        return cls(
            config=config,
            workspace=workspace,
            bus=bus,
            fallback_manager=fallback_manager,
            model=model,
            max_iterations=max_iterations,
            context_window_tokens=context_window_tokens,
            channels_config=channels_config,
            web_search_config=resolved_configs["web_search"],
            web_proxy=web_proxy,
            exec_config=resolved_configs["exec"],
            filesystem_config=resolved_configs["filesystem"],
            memory_config=resolved_configs["memory"],
            code_execution_config=resolved_configs["code_execution"],
            cron_service=cron_service,
            restrict_to_workspace=restrict_to_workspace,
            timezone=timezone,
            compaction_config=compaction_config,
            max_budget_usd=max_budget_usd,
            warn_threshold_usd=warn_threshold_usd,
            budget_config=budget_config,
            mcp_servers=mcp_servers,
            tools=tools,
            skill_registry=skill_registry,
            guardrail_manager=guardrail_manager,
            sessions=sessions,
            subagents=subagents,
            memory_manager=memory_manager,
            compactor=compactor,
            cost_tracker=cost_tracker,
            mcp=mcp,
            context_builder=context_builder,
            pipeline=pipeline,
            commands=session_ext["commands"],
            tool_executor=session_ext["tool_executor"],
            bootstrap_hook=bootstrap_hook,
            compaction_hook=compaction_hook,
            daily_log=daily_log,
            interaction_log=interaction_log,
            memory_search_tool=binder.memory_search_tool,
            question_tool=binder.question_tool,
            handoff_manager=session_ext["handoff_manager"],
            session_bootstrap=session_ext["session_bootstrap"],
            task_tracker=session_ext["task_tracker"],
            memory_encoder=session_ext["memory_encoder"],
            app_state=session_ext["app_state"],
            # ------------------------------------------------------------------
            # Cache layer — always construct fresh per process so the
            # cross-session disk cache in prompt_persist is the only
            # persistent piece.  See markbot.agent.{prefix_cache,
            # llm_response_cache, token_estimate_cache}.
            # ------------------------------------------------------------------
            prefix_stability=PrefixStabilityManager(),
            llm_response_cache=LLMResponseCache(),
            token_estimate_cache=TokenEstimateCache(),
            _init_timings=timings,
        )

    # ------------------------------------------------------------------
    # Sub-builders — each handles one concern so the orchestration
    # method above stays readable.  Kept as classmethods so they can
    # be unit-tested in isolation.
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_model(config: "Config | None") -> tuple[str, Any]:
        """Resolve the primary model name and provider config."""
        model = "unknown"
        primary_provider_config = None
        if config and config.primary_model_ref:
            primary_provider, primary_model = config.resolve_model(config.primary_model_ref)
            model = primary_model.name
            primary_provider_config = primary_provider
        return model, primary_provider_config

    @staticmethod
    def _resolve_tool_configs(
        web_search_config: "WebSearchConfig | None",
        exec_config: "ExecToolConfig | None",
        filesystem_config: "FilesystemToolConfig | None",
        memory_config: "MemoryToolsConfig | None",
        config: "Config | None",
    ) -> dict[str, Any]:
        """Resolve tool configs to their defaults when not supplied."""
        from markbot.config.schema import (
            CodeExecutionConfig as _CEC,
            ExecToolConfig as _ETC,
            FilesystemToolConfig as _FTC,
            MemoryToolsConfig as _MTC,
            WebSearchConfig as _WSC,
        )
        return {
            "web_search": web_search_config or _WSC(),
            "exec": exec_config or _ETC(),
            "filesystem": filesystem_config or _FTC(),
            "memory": memory_config or _MTC(),
            "code_execution": getattr(
                config.tools if config else None, "code_execution", None
            ) or _CEC(),
        }

    @staticmethod
    def _build_cost_and_compaction(
        fallback_manager: "FallbackManager",
        compaction_config: "CompactionConfig | None",
        budget_config: "BudgetConfig | None",
        max_budget_usd: float | None,
        warn_threshold_usd: float,
        timings: dict[str, float],
    ) -> tuple[Any, Any]:
        """Build the MultiLevelCompactor and CostTracker."""
        from markbot.agent.compact import CompactionConfig as _CC
        from markbot.agent.compact import MultiLevelCompactor as _MLC
        from markbot.agent.cost import CostTracker as _CT
        from markbot.agent.cost import ModelPricing as _MP
        from markbot.agent.cost import PricingTable as _PT

        _t0 = time.time()
        _compaction_cfg = compaction_config or _CC()
        compactor = _MLC(fallback_manager=fallback_manager, config=_compaction_cfg)
        timings["compactor"] = time.time() - _t0

        pricing: _PT | None = None
        if budget_config and getattr(budget_config, "custom_pricing", None):
            _custom = {k: _MP(**v) for k, v in budget_config.custom_pricing.items()}
            pricing = _PT(custom=_custom)
        cost_tracker = _CT(
            max_budget_usd=max_budget_usd,
            warn_threshold_usd=warn_threshold_usd,
            pricing=pricing,
        )
        return compactor, cost_tracker

    @staticmethod
    def _build_tools_and_skills(
        workspace: Path | None,
        timings: dict[str, float],
    ) -> tuple[Any, Any, Any]:
        """Build ToolRegistry, SkillRegistry, and guardrail manager."""
        from markbot.skills import SkillRegistry as _SR
        from markbot.skills.core.guardrail import SkillGuardrailManager as _SGM
        from markbot.tools.registry import ToolRegistry as _TR

        _t0 = time.time()
        tools = _TR()
        timings["tools"] = time.time() - _t0

        _t0 = time.time()
        skill_registry = _SR(workspace, tool_registry=tools)
        skill_registry.load_all()
        timings["skill_registry"] = time.time() - _t0

        guardrail_manager = _SGM(context="main")
        return tools, skill_registry, guardrail_manager

    @staticmethod
    def _build_sessions_subagents_mcp(
        fallback_manager: "FallbackManager",
        config: "Config | None",
        workspace: Path | None,
        bus: "MessageBus",
        model: str,
        resolved_configs: dict[str, Any],
        restrict_to_workspace: bool,
        cost_tracker: "CostTracker",
        session_manager: "SessionManager | None",
        mcp_servers: dict | None,
        timings: dict[str, float],
    ) -> tuple[Any, Any, Any]:
        """Build SessionManager, SubagentManager, and McpManager."""
        from markbot.agent.mcp.manager import McpManager as _MM
        from markbot.agent.subagent import SubagentManager as _SM
        from markbot.session.session import SessionManager as _SM2

        _t0 = time.time()
        sessions = session_manager or _SM2(workspace)
        subagents = _SM(
            fallback_manager=fallback_manager,
            config=config,
            workspace=workspace,
            bus=bus,
            model=model,
            web_search_config=resolved_configs["web_search"],
            web_proxy=resolved_configs.get("web_proxy"),
            exec_config=resolved_configs["exec"],
            filesystem_config=resolved_configs["filesystem"],
            restrict_to_workspace=restrict_to_workspace,
            cost_tracker=cost_tracker,
        )
        timings["subagents"] = time.time() - _t0

        mcp = _MM(mcp_servers)
        return sessions, subagents, mcp

    @staticmethod
    def _build_memory(
        workspace: Path | None,
        fallback_manager: "FallbackManager",
        model: str,
        primary_provider_config: Any,
        resolved_configs: dict[str, Any],
        timezone: str | None,
        timings: dict[str, float],
    ) -> "BaseMemoryManager":
        """Build the MemoryManager with embedding + LLM configs."""
        from markbot.memory.manager import MemoryManager as _HMM

        memory_cfg = resolved_configs["memory"]
        embedding_config: dict[str, Any] = {}
        if memory_cfg:
            embedding_config = {
                "backend": getattr(memory_cfg, "embedding_backend", "openai"),
                "api_key": getattr(memory_cfg, "embedding_api_key", ""),
                "base_url": getattr(memory_cfg, "embedding_base_url", ""),
                "model_name": getattr(memory_cfg, "embedding_model_name", ""),
            }

        llm_config: dict[str, Any] = {
            "backend": "openai",
            "api_key": getattr(primary_provider_config, 'api_key', '') or "",
            "base_url": getattr(primary_provider_config, 'api_base', '') or "",
            "model_name": model,
        }

        _t0 = time.time()
        memory_manager = _HMM(
            working_dir=str(workspace),
            agent_id="markbot",
            fallback_manager=fallback_manager,
            model=model,
            embedding_config=embedding_config,
            llm_config=llm_config,
            language="zh",
            timezone=timezone,
            context_compact_enabled=getattr(memory_cfg, "context_compact_enabled", True),
            memory_compact_ratio=getattr(memory_cfg, "memory_compact_ratio", 0.75),
            memory_reserve_ratio=getattr(memory_cfg, "memory_reserve_ratio", 0.1),
            compact_with_thinking_block=True,
            memory_summary_enabled=getattr(memory_cfg, "memory_summary_enabled", True),
            force_memory_search=getattr(memory_cfg, "force_memory_search", False),
            force_max_results=getattr(memory_cfg, "force_max_results", 1),
            force_min_score=getattr(memory_cfg, "force_min_score", 0.3),
            long_term_enabled=getattr(memory_cfg, "long_term_enabled", True),
            vector_backend=getattr(memory_cfg, "vector_backend", "sqlite"),
            vector_max_records=getattr(memory_cfg, "vector_max_records", 50_000),
            vector_max_scan_records=getattr(memory_cfg, "vector_max_scan_records", 20_000),
            vector_min_content_chars=getattr(memory_cfg, "vector_min_content_chars", 12),
            vector_top_k_multiplier=getattr(memory_cfg, "vector_top_k_multiplier", 2),
            vector_min_score=getattr(memory_cfg, "vector_min_score", 0.15),
            daily_log_retention_days=getattr(memory_cfg, "daily_log_retention_days", 30),
            consolidation_enabled=getattr(memory_cfg, "consolidation_enabled", True),
            consolidation_dedup_threshold=getattr(memory_cfg, "consolidation_dedup_threshold", 0.95),
            consolidation_age_decay_days=getattr(memory_cfg, "consolidation_age_decay_days", 90.0),
            consolidation_promote_access=getattr(memory_cfg, "consolidation_promote_access", 5),
            provider_config=getattr(memory_cfg, "provider_config", None),
        )
        timings["memory_manager"] = time.time() - _t0
        return memory_manager

    @staticmethod
    def _build_hooks_and_context(
        workspace: Path | None,
        memory_manager: "BaseMemoryManager",
        memory_cfg: "MemoryToolsConfig | None",
        compaction_config: "CompactionConfig | None",
        context_window_tokens: int,
        timezone: str | None,
        tools: "ToolRegistry",
        skill_registry: "SkillRegistry",
    ) -> tuple[Any, Any, Any]:
        """Build BootstrapHook, MemoryCompactionHook, and ContextBuilder."""
        from markbot.agent.context import ContextBuilder as _CB
        from markbot.agent.hooks.bootstrap import BootstrapHook as _BH
        from markbot.agent.hooks.compaction import MemoryCompactionHook as _MCH

        bootstrap_hook = _BH(working_dir=workspace, language="zh")

        memory_compact_threshold = int(context_window_tokens * 0.75)
        compaction_hook = _MCH(
            memory_manager=memory_manager,
            memory_compact_threshold=memory_compact_threshold,
            memory_compact_reserve=getattr(memory_cfg, "memory_compact_reserve", 10000),
            context_compact_enabled=getattr(memory_cfg, "context_compact_enabled", True),
            memory_summary_enabled=getattr(memory_cfg, "memory_summary_enabled", True),
        )

        _system_prompt_token_budget = 16_000
        if compaction_config and hasattr(compaction_config, "system_prompt_token_budget"):
            _system_prompt_token_budget = compaction_config.system_prompt_token_budget

        context_builder = _CB(
            workspace,
            timezone=timezone,
            tool_registry=tools,
            skill_registry=skill_registry,
            memory_manager=memory_manager,
            system_prompt_token_budget=_system_prompt_token_budget,
        )
        return bootstrap_hook, compaction_hook, context_builder

    @staticmethod
    def _build_tool_binder(
        tools: "ToolRegistry",
        workspace: Path | None,
        restrict_to_workspace: bool,
        resolved_configs: dict[str, Any],
        config: "Config | None",
        cron_service: "CronService | None",
        subagents: "SubagentManager",
        memory_manager: "BaseMemoryManager",
        skill_registry: "SkillRegistry",
        timezone: str | None,
        bus: "MessageBus",
        timings: dict[str, float],
    ) -> Any:
        """Build and register all tools via ToolBinder."""
        from markbot.agent.tool_binder import ToolBinder as _TB

        _allowed_dir = workspace if restrict_to_workspace else None
        _t0 = time.time()
        binder = _TB(
            tools,
            workspace=workspace,
            allowed_dir=_allowed_dir,
            web_search_config=resolved_configs["web_search"],
            web_proxy=resolved_configs.get("web_proxy"),
            exec_config=resolved_configs["exec"],
            filesystem_config=resolved_configs["filesystem"],
            code_execution_config=resolved_configs["code_execution"],
            computer_use_config=getattr(config.tools if config else None, "computer_use", None),
            browser_config=getattr(config.tools if config else None, "browser", None),
            cron_service=cron_service,
            subagent_manager=subagents,
            memory_manager=memory_manager,
            skill_registry=skill_registry,
            timezone=timezone,
            publish_outbound=bus.publish_outbound,
        )
        binder.register_all()
        timings["tool_binder"] = time.time() - _t0
        return binder

    @staticmethod
    def _build_pipeline(
        binder: Any,
        memory_manager: "BaseMemoryManager",
        sessions: "SessionManager",
        workspace: Path | None,
    ) -> tuple[Any, Any, Any]:
        """Build the MessagePipeline with middlewares + daily/interaction logs."""
        from markbot.agent.pipeline.engine import MessagePipeline as _MP2
        from markbot.agent.pipeline.middleware import (
            MemoryLifecycleMiddleware as _MLM,
        )
        from markbot.agent.pipeline.middleware import (
            QuestionResponseMiddleware as _QRM,
        )
        from markbot.agent.services.interaction import InteractionLogger as _IL
        from markbot.memory.daily_log import DailyLogManager as _DLM

        question_tool = binder.question_tool
        pipeline = _MP2()
        pipeline.use(_QRM(get_question_tool=lambda: question_tool))
        daily_log = _DLM(workspace=workspace)
        interaction_log = _IL()
        if hasattr(memory_manager, "_daily_log"):
            memory_manager._daily_log = daily_log
        pipeline.use(
            _MLM(
                memory_manager=memory_manager,
                daily_log=daily_log,
                session_manager=sessions,
            )
        )
        return pipeline, daily_log, interaction_log

    @staticmethod
    def _build_session_extensions(
        workspace: Path | None,
        memory_manager: "BaseMemoryManager",
        mcp: "McpManager",
        tools: "ToolRegistry",
        timings: dict[str, float],
        config: "Config | None" = None,
    ) -> dict[str, Any]:
        """Build handoff, task tracker, memory encoder, bootstrap, commands."""
        from markbot.cli.slash_commands import CommandRouter as _CR
        from markbot.cli.slash_commands import register_builtin_commands as _rbc
        from markbot.agent.services.executor import ToolExecutor as _TE
        from markbot.memory.encoder import MemoryEncoder as _ME
        from markbot.session.app_state import AppStateProvider as _ASP
        from markbot.session.bootstrap import SessionBootstrap as _SB
        from markbot.session.handoff import HandoffManager as _HM
        from markbot.session.task_tracker import TaskTracker as _TT
        from markbot.types.permission import PermissionMode as _PM

        _t0 = time.time()
        handoff_manager = _HM(workspace)
        task_tracker = _TT(workspace)
        memory_encoder = _ME(workspace, memory_store=getattr(memory_manager, "_memory_store", None))
        app_state = _ASP.initialize()
        # Apply the configured default permission mode so interactive turns
        # start in the mode the user chose (schema default is ``auto`` so
        # mutating tools work out of the box). ``/mode`` still overrides at
        # runtime; cron/autopilot/heartbeat force AUTO via process_direct.
        # The ``!= "default"`` skip avoids a redundant call when the user
        # explicitly opts into ``default`` (which leaves AppStateProvider at
        # its own DEFAULT — they take responsibility for wiring a UI
        # confirmation handler for the ``ask`` permission decision).
        configured_mode = None
        try:
            configured_mode = config.agents.defaults.default_permission_mode if config else None
        except AttributeError:
            configured_mode = None
        if configured_mode and configured_mode != "default":
            app_state.set_permission_mode(_PM(configured_mode))
        session_bootstrap = _SB(
            workspace,
            handoff_manager=handoff_manager,
            mcp_manager=mcp,
            task_tracker=task_tracker,
        )
        commands = _CR()
        _rbc(commands)
        tool_executor = _TE(tools)
        timings["session_extensions"] = time.time() - _t0
        return {
            "handoff_manager": handoff_manager,
            "task_tracker": task_tracker,
            "memory_encoder": memory_encoder,
            "app_state": app_state,
            "session_bootstrap": session_bootstrap,
            "commands": commands,
            "tool_executor": tool_executor,
        }

    def record_timing(self, component: str, elapsed_s: float) -> None:
        self._init_timings[component] = elapsed_s

    @property
    def init_summary(self) -> str:
        if not self._init_timings:
            return "No timing data"
        lines = [f"  {k}: {v:.3f}s" for k, v in sorted(self._init_timings.items())]
        total = sum(self._init_timings.values())
        lines.append(f"  TOTAL: {total:.3f}s")
        return "\n".join(lines)
