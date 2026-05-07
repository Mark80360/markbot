"""Spawn tool for creating background subagents."""

from typing import TYPE_CHECKING, Any, Optional

from markbot.tools.base import BaseTool
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
        self._session_key = f"{channel}:{chat_id}"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="spawn",
            description=(
                "Spawn a subagent to handle a task in the background. "
                "Use this for complex or time-consuming tasks that can run independently. "
                "The subagent will complete the task and report back when done. "
                "For deliverables or existing projects, inspect the workspace first "
                "and use a dedicated subdirectory when helpful."
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

        channel = context.channel or self._origin_channel
        chat_id = context.chat_id or self._origin_chat_id
        session_key = f"{channel}:{chat_id}" if channel and chat_id else self._session_key

        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=channel,
            origin_chat_id=chat_id,
            session_key=session_key,
        )
