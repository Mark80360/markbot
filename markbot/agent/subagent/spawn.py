"""Spawn tool for creating background subagents."""

from typing import TYPE_CHECKING, Any

from loguru import logger

from markbot.tools.base import BaseTool
from markbot.bus.events import make_session_key
from markbot.types.permission import PermissionDecision
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from markbot.agent.subagent.manager import SubagentManager


class SpawnTool(BaseTool):
    """Tool to spawn a subagent for background task execution."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._session_key = "cli:direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._session_key = make_session_key(channel, chat_id) or "cli:direct"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="spawn",
            description=(
                "Spawn a subagent to handle a task in the background. "
                "Use this for complex or time-consuming tasks that can run independently. "
                "The subagent will complete the task and report back when done. "
                "For deliverables or existing projects, inspect the workspace first "
                "and use a dedicated subdirectory when helpful. "
                "By default the subagent is read-only. To grant extra tools or "
                "relax the budget/timeout, pass a `capability` object; the subagent "
                "will be limited to `allowed_tools` and cannot call any tool in "
                "`forbidden_tools`."
            ),
            parameters=[
                ToolParameter(
                    name="task",
                    type="string",
                    description="The task for the subagent to complete",
                    required=True,
                ),
                ToolParameter(
                    name="label",
                    type="string",
                    description="Optional short label for the task (for display)",
                    required=False,
                ),
                ToolParameter(
                    name="capability",
                    type="object",
                    description=(
                        "Optional capability object declaring what the subagent "
                        "may do. Keys (snake_case or camelCase): "
                        "allowed_tools (list[str]), forbidden_tools (list[str]), "
                        "max_iterations (int, default 15), max_budget_usd "
                        "(number), timeout_seconds (number), description (str). "
                        "Omit or pass null to use the default read-only profile."
                    ),
                    required=False,
                ),
            ],
            is_read_only=False,
            is_destructive=False,
        )

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        return PermissionDecision(behavior="ask")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> str:
        task = params["task"]
        label = params.get("label")
        capability_param = params.get("capability")

        channel = context.channel or self._origin_channel
        chat_id = context.chat_id or self._origin_chat_id
        session_key = make_session_key(channel, chat_id) or self._session_key

        from markbot.agent.subagent.capability import CapabilityToken

        try:
            capability = CapabilityToken.from_dict(capability_param)
        except (TypeError, ValueError) as e:
            logger.warning("SpawnTool: invalid capability payload, falling back to read-only: {}", e)
            capability = CapabilityToken.read_only(
                description="Invalid capability payload — read-only fallback"
            )

        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=channel,
            origin_chat_id=chat_id,
            session_key=session_key,
            capability=capability,
        )
