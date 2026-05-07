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
    from markbot.agent.tool_binder import ToolBinder
    from markbot.bus.queue import MessageBus
    from markbot.cli.slash_commands import CommandRouter
    from markbot.config.schema import (
        ChannelsConfig,
        CompactionConfig as SchemaCompactionConfig,
        Config,
        ExecToolConfig,
        FilesystemToolConfig,
        MemoryToolsConfig,
        WebSearchConfig,
    )
    from markbot.memory.base import BaseMemoryManager
    from markbot.memory.daily_log import DailyLogManager
    from markbot.providers.fallback import FallbackManager
    from markbot.schedule.cron import CronService
    from markbot.session.session import SessionManager
    from markbot.skills import SkillRegistry
    from markbot.skills.core.guardrail import SkillGuardrailManager
    from markbot.tools.memory_tools import MemorySearchTool
    from markbot.tools.message import MessageTool
    from markbot.tools.question import QuestionTool
    from markbot.tools.registry import ToolRegistry


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

    Built via ``AgentContext.builder()`` — never instantiate directly.
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
    cron_service: "CronService | None" = None
    restrict_to_workspace: bool = False
    timezone: str | None = None
    compaction_config: "CompactionConfig | None" = None
    max_budget_usd: float | None = None
    warn_threshold_usd: float = 0.5
    budget_config: Any = None

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
    question_tool: "QuestionTool | None" = None
    message_tool: "MessageTool | None" = None

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

        def with_budget(self, max_usd: float | None, warn_usd: float = 0.5, budget_config: Any = None) -> "AgentContext.Builder":
            self._params["max_budget_usd"] = max_usd
            self._params["warn_threshold_usd"] = warn_usd
            self._params["budget_config"] = budget_config
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
