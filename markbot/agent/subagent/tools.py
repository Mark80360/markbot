"""Tool for checking subagent progress and output."""

from typing import TYPE_CHECKING, Any

from markbot.tools.base import BaseTool
from markbot.types.permission import PermissionDecision
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from markbot.agent.subagent.manager import SubagentManager


class CheckSubagentTool(BaseTool):
    """Tool to check subagent progress and output."""

    def __init__(self, subagent_manager: "SubagentManager"):
        self._manager = subagent_manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="check_subagent",
            description=(
                "Check the progress or output of a running or completed subagent task. "
                "Use this to get status updates, view partial results, or read the full output log."
            ),
            parameters=[
                ToolParameter(
                    name="task_id",
                    type="string",
                    description="The ID of the subagent task to check",
                    required=True,
                ),
                ToolParameter(
                    name="action",
                    type="string",
                    description="What to check: 'status' for progress summary, 'output' for full output, 'tail' for last 50 lines",
                    required=True,
                    enum=["status", "output", "tail"],
                ),
            ],
            is_read_only=True,
            is_destructive=False,
        )

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> str:
        task_id = params["task_id"]
        action = params.get("action", "status")
        progress_manager = self._manager.progress_manager

        if progress_manager is None:
            return (
                f"Task {task_id}: progress tracking is unavailable (no workspace configured). "
                "Subagent results are still announced when tasks complete."
            )

        if action == "status":
            progress = progress_manager.get_progress(task_id)

            if progress:
                status_lines = [
                    f"Task: {task_id}",
                    f"Status: {progress.status}",
                    f"Duration: {progress.duration_seconds:.1f}s",
                    f"Tool Uses: {progress.tool_use_count}",
                    f"Total Tokens: {progress.total_tokens}",
                ]

                if progress.summary:
                    status_lines.append(f"Summary: {progress.summary}")

                if progress.last_activity:
                    status_lines.append(f"Last Activity: {progress.last_activity.description}")

                if progress.recent_activities:
                    status_lines.append("\nRecent Activities:")
                    for activity in progress.recent_activities[-5:]:
                        status_lines.append(f"  - {activity.description}")

                output_file = progress_manager.get_output_file(task_id)
                if output_file:
                    status_lines.append(f"\nOutput File: {output_file}")

                return "\n".join(status_lines)

            summary = progress_manager.get_task_summary(task_id)
            if summary:
                return (
                    f"Task: {task_id}\n"
                    f"Status: {summary['status']} (completed)\n"
                    f"Output File: {summary['output_file']}\n"
                    f"File Size: {summary['file_size']} bytes\n\n"
                    f"Use action='output' or action='tail' to view the full output."
                )

            return f"Task {task_id} not found. It may never have existed or its output has been cleaned up."

        elif action == "output":
            content = await progress_manager.read_output(task_id, max_bytes=100_000)
            if not content:
                return f"No output found for task {task_id}."
            return content

        elif action == "tail":
            content = await progress_manager.tail_output(task_id, lines=50)
            if not content:
                return f"No output found for task {task_id}."
            return content

        else:
            return f"Unknown action: {action}. Use 'status', 'output', or 'tail'."


class ListSubagentsTool(BaseTool):
    """Tool to list active subagents."""

    def __init__(self, subagent_manager: "SubagentManager"):
        self._manager = subagent_manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_subagents",
            description=(
                "List all currently running subagent tasks with their progress summary. "
                "Use this to see what background tasks are active."
            ),
            parameters=[],
            is_read_only=True,
            is_destructive=False,
        )

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> str:
        progress_manager = self._manager.progress_manager

        if progress_manager is None:
            return "Progress tracking is unavailable (no workspace configured)."

        active_tasks = progress_manager.list_active_tasks()

        if not active_tasks:
            return "No active subagent tasks."

        lines = [f"Active Subagent Tasks ({len(active_tasks)}):\n"]

        for task in active_tasks:
            lines.append(f"Task: {task.task_id}")
            lines.append(f"  Status: {task.status}")
            lines.append(f"  Duration: {task.duration_seconds:.1f}s")
            lines.append(f"  Tools: {task.tool_use_count} | Tokens: {task.total_tokens}")
            if task.last_activity:
                lines.append(f"  Last: {task.last_activity.description}")
            lines.append("")

        return "\n".join(lines)
