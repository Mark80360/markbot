"""Tool registration for the agent loop.

Extracted from AgentLoop so that tool setup does not bloat the core loop class.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from markbot.skills.core.loader import BUILTIN_SKILLS_DIR

if TYPE_CHECKING:
    from markbot.agent.subagent import SubagentManager
    from markbot.memory.manager import MemoryManager
    from markbot.schedule.cron import CronService
    from markbot.skills import SkillRegistry
    from markbot.tools.registry import ToolRegistry


class ToolBinder:
    """Creates and registers all default tools used by the agent loop."""

    def __init__(
        self,
        tools: ToolRegistry,
        *,
        workspace: Path,
        allowed_dir: Path | None = None,
        web_search_config: Any = None,
        web_proxy: str | None = None,
        exec_config: Any = None,
        filesystem_config: Any = None,
        code_execution_config: Any = None,
        computer_use_config: Any = None,
        browser_config: Any = None,
        cron_service: "CronService | None" = None,
        subagent_manager: "SubagentManager | None" = None,
        memory_manager: "MemoryManager | None" = None,
        skill_registry: "SkillRegistry | None" = None,
        timezone: str | None = None,
        publish_outbound: Any = None,
        profile: Any = None,
    ):
        self._tools = tools
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._web_search_config = web_search_config
        self._web_proxy = web_proxy
        self._exec_config = exec_config
        self._filesystem_config = filesystem_config
        self._code_execution_config = code_execution_config
        self._computer_use_config = computer_use_config
        self._browser_config = browser_config
        self._cron_service = cron_service
        self._subagent_manager = subagent_manager
        self._memory_manager = memory_manager
        self._skill_registry = skill_registry
        self._timezone = timezone
        self._publish_outbound = publish_outbound
        self._profile = profile

        self._question_tool: Any = None
        self._memory_search_tool: Any = None

    @property
    def question_tool(self) -> Any:
        return self._question_tool

    @property
    def memory_search_tool(self) -> Any:
        return self._memory_search_tool

    def register_all(self) -> None:
        """Register the full default tool set, filtered by profile when set."""
        extra_read = [BUILTIN_SKILLS_DIR] if self._allowed_dir else None
        profile = self._profile

        self._register_base_tools(extra_allowed_dirs=extra_read)
        self._register_agent_tools()
        self._register_context_tools()
        if profile is None or getattr(profile, "enable_subagents", True):
            self._register_subagent_tools()
        self._register_memory_tools()
        self._register_skill_tools()
        if profile is None or getattr(profile, "enable_autopilot", True):
            self._register_autopilot_tools()
        self._register_desktop_tools()

        if self._cron_service:
            from markbot.tools.cron import CronTool

            self._tools.register(CronTool(self._cron_service, default_timezone=self._timezone or "UTC"))

        # Soft-disable tools the profile opts out of (exact names + group flags).
        # Prefer soft-disable over unregister so mid-session rebind / deferred
        # mutations can re-enable without re-importing tool classes.
        from markbot.agent.footprint import ToolFootprint, apply_footprint_to_registry

        footprint = ToolFootprint()
        footprint.apply_profile(profile)
        apply_footprint_to_registry(self._tools, footprint)
        self._footprint = footprint

    def _register_base_tools(self, extra_allowed_dirs: list[Path] | None = None) -> None:
        """Register filesystem, web, shell, and search tools."""
        from markbot.tools.filesystem import (
            DeleteFileTool,
            EditFileTool,
            ListDirTool,
            ReadFileTool,
            WriteFileTool,
        )
        from markbot.tools.search import GlobTool, GrepTool
        from markbot.tools.shell import ExecTool
        from markbot.tools.web import WebExtractTool, WebFetchTool, WebSearchTool

        fs_backup_dir = (
            getattr(self._filesystem_config, "backup_dir", None) if self._filesystem_config else None
        )
        fs_max_backups = (
            getattr(self._filesystem_config, "max_backups", None) if self._filesystem_config else None
        )
        fs_safe_delete = (
            getattr(self._filesystem_config, "safe_delete", True) if self._filesystem_config else True
        )

        self._tools.register(
            ReadFileTool(
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
                extra_allowed_dirs=extra_allowed_dirs,
            )
        )
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            self._tools.register(
                cls(
                    workspace=self._workspace,
                    allowed_dir=self._allowed_dir,
                    backup_dir=fs_backup_dir,
                    max_backups=fs_max_backups,
                )
            )
        self._tools.register(
            DeleteFileTool(
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
                backup_dir=fs_backup_dir,
                max_backups=fs_max_backups,
                safe_delete=fs_safe_delete,
            )
        )

        if self._exec_config and self._exec_config.enable:
            profile = self._profile
            restrict = getattr(self._exec_config, "restrict_to_workspace", True)
            if profile is not None and getattr(profile, "exec_restrict_to_workspace", False):
                restrict = True
            require_allowlist = bool(getattr(self._exec_config, "require_allowlist", False))
            if profile is not None and getattr(profile, "exec_require_allowlist", False):
                require_allowlist = True
            allow_patterns = list(getattr(self._exec_config, "allow_patterns", None) or [])
            deny_patterns = list(getattr(self._exec_config, "deny_patterns", None) or [])
            # Safe default allowlist for assistant/unattended when none configured.
            if require_allowlist and not allow_patterns:
                allow_patterns = [
                    r"^(ls|pwd|cat|head|tail|wc|echo|date|uname|whoami|which|file|stat)\b",
                    r"^(git|rg|grep|find|sed|awk|sort|uniq|diff|python3?|pip3?|pytest|node|npm|pnpm|yarn|make|cargo|go|rustc)\b",
                    r"^(mkdir|touch|cp|mv|chmod|ln|tar|zip|unzip|curl|wget)\b",
                ]
            self._tools.register(
                ExecTool(
                    working_dir=str(self._workspace),
                    timeout=self._exec_config.timeout,
                    restrict_to_workspace=restrict,
                    require_allowlist=require_allowlist,
                    allow_patterns=allow_patterns,
                    deny_patterns=deny_patterns,
                    path_append=self._exec_config.path_append,
                    allowed_internal_ips=self._exec_config.allowed_internal_ips,
                )
            )

        self._tools.register(WebSearchTool(config=self._web_search_config, proxy=self._web_proxy))
        self._tools.register(WebFetchTool(proxy=self._web_proxy))
        self._tools.register(WebExtractTool(proxy=self._web_proxy))
        self._tools.register(GlobTool(workspace=self._workspace, allowed_dir=self._allowed_dir))
        self._tools.register(GrepTool(workspace=self._workspace, allowed_dir=self._allowed_dir))

        if self._code_execution_config is None or self._code_execution_config.enable:
            from markbot.tools.code import CodeExecutionTool

            self._tools.register(
                CodeExecutionTool(
                    config=self._code_execution_config,
                    skill_registry=self._skill_registry,
                )
            )

    def _register_agent_tools(self) -> None:
        """Register tools specific to agent operation."""
        from markbot.agent.subagent.spawn import SpawnTool
        from markbot.tools.explore import ExploreTool
        from markbot.tools.message import MessageTool
        from markbot.tools.think import ThinkTool
        from markbot.tools.todo import TodoTool

        self._tools.register(MessageTool(send_callback=self._publish_outbound))
        if self._subagent_manager and (
            self._profile is None or getattr(self._profile, "enable_subagents", True)
        ):
            self._tools.register(SpawnTool(manager=self._subagent_manager))
        self._tools.register(ThinkTool())
        self._tools.register(TodoTool(workspace=self._workspace))
        if self._profile is None or getattr(self._profile, "enable_explore", True):
            self._tools.register(ExploreTool(workspace=self._workspace, allowed_dir=self._allowed_dir))

    def _register_context_tools(self) -> None:
        """Register context explorer tools for AI-driven dynamic loading."""
        from markbot.tools.context_explorer import (
            ExploreContextCatalogTool,
            LoadContextTool,
            SearchContextTool,
        )

        self._tools.register(ExploreContextCatalogTool(
            workspace=self._workspace, memory_manager=self._memory_manager,
        ))
        self._tools.register(SearchContextTool(
            workspace=self._workspace, memory_manager=self._memory_manager,
        ))
        self._tools.register(LoadContextTool(
            workspace=self._workspace, memory_manager=self._memory_manager,
        ))

    def _register_subagent_tools(self) -> None:
        """Register subagent management tools."""
        from markbot.agent.subagent.tools import CheckSubagentTool, ListSubagentsTool

        if self._subagent_manager:
            self._tools.register(CheckSubagentTool(subagent_manager=self._subagent_manager))
            self._tools.register(ListSubagentsTool(subagent_manager=self._subagent_manager))

    def _register_memory_tools(self) -> None:
        """Register memory search and self-management tools."""
        from markbot.tools.memory import MemorySearchTool
        from markbot.tools.memory_tools import (
            MemoryForgetTool,
            MemoryListTool,
            MemorySaveTool,
        )
        from markbot.tools.question import AskUserQuestionTool

        if self._memory_manager:
            self._memory_search_tool = MemorySearchTool(memory_manager=self._memory_manager)
            self._tools.register(self._memory_search_tool)
            self._tools.register(MemorySaveTool(memory_manager=self._memory_manager))
            self._tools.register(MemoryForgetTool(memory_manager=self._memory_manager))
            self._tools.register(MemoryListTool(memory_manager=self._memory_manager))

        self._question_tool = AskUserQuestionTool(send_callback=self._publish_outbound)
        self._question_tool.set_context("", "")
        self._tools.register(self._question_tool)

    def _register_skill_tools(self) -> None:
        """Register skill progressive disclosure tools and script tools."""
        from markbot.skills import SkillsListTool, SkillViewTool
        from markbot.skills.core.manage import SkillManageTool

        if self._skill_registry:
            self._tools.register(SkillViewTool(registry=self._skill_registry))
            self._tools.register(SkillsListTool(registry=self._skill_registry))
            self._skill_registry.register_script_tools()
            self._tools.register(
                SkillManageTool(workspace=self._workspace, skill_registry=self._skill_registry)
            )
        else:
            self._tools.register(SkillManageTool(workspace=self._workspace))

    def _register_autopilot_tools(self) -> None:
        """Register autopilot pipeline tools."""
        from markbot.autopilot.tools import ALL_AUTOPILOT_TOOLS

        for tool_cls in ALL_AUTOPILOT_TOOLS:
            self._tools.register(tool_cls())

    def _register_desktop_tools(self) -> None:
        """Register computer_use and browser automation tools."""
        profile = self._profile
        enable_desktop = profile is None or getattr(profile, "enable_desktop", True)
        enable_browser = profile is None or getattr(profile, "enable_browser", True)

        if enable_desktop and (self._computer_use_config is None or self._computer_use_config.enable):
            from markbot.tools.computer_use.tool import ComputerUseTool

            tool = ComputerUseTool(config=self._computer_use_config)
            # Service-gated: only enter the registry (and thus the model
            # schema / API tool footprint) when a backend is actually usable.
            self._tools.register_gated(tool)

        if enable_browser and (self._browser_config is None or self._browser_config.enable):
            from markbot.tools.browser import BROWSER_TOOLS

            for browser_tool in BROWSER_TOOLS:
                # Prefer register_gated so unavailable browser backends do not
                # inflate every API call's tool list.
                if hasattr(browser_tool, "is_available") or hasattr(browser_tool, "is_enabled"):
                    self._tools.register_gated(browser_tool)
                else:
                    self._tools.register(browser_tool)

