"""Tool for checking subagent progress and output."""

from typing import TYPE_CHECKING, Any

from markbot.tools.base import Tool

if TYPE_CHECKING:
    from markbot.subagent import SubagentManager


class CheckSubagentTool(Tool):
    """Tool to check subagent progress and output."""

    def __init__(self, subagent_manager: "SubagentManager"):
        self._manager = subagent_manager

    @property
    def name(self) -> str:
        return "check_subagent"

    @property
    def description(self) -> str:
        return (
            "Check the progress or output of a running or completed subagent task. "
            "Use this to get status updates, view partial results, or read the full output log."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The ID of the subagent task to check",
                },
                "action": {
                    "type": "string",
                    "enum": ["status", "output", "tail"],
                    "description": "What to check: 'status' for progress summary, 'output' for full output, 'tail' for last 50 lines",
                },
            },
            "required": ["task_id", "action"],
        }

    async def _legacy_execute(self, task_id: str, action: str = "status", **kwargs: Any) -> str:
        """Check subagent progress or output."""
        progress_manager = self._manager.progress_manager
        
        if action == "status":
            # Try to get live progress first
            progress = progress_manager.get_progress(task_id)
            
            if progress:
                # Task is still running or recently completed
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
            
            # Try to get summary from output file (completed task)
            summary = progress_manager.get_task_summary(task_id)
            if summary:
                return f"""Task: {task_id}
Status: {summary['status']} (completed)
Output File: {summary['output_file']}
File Size: {summary['file_size']} bytes

Use action='output' or action='tail' to view the full output."""
            
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


class ListSubagentsTool(Tool):
    """Tool to list active subagents."""

    def __init__(self, subagent_manager: "SubagentManager"):
        self._manager = subagent_manager

    @property
    def name(self) -> str:
        return "list_subagents"

    @property
    def description(self) -> str:
        return (
            "List all currently running subagent tasks with their progress summary. "
            "Use this to see what background tasks are active."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def _legacy_execute(self, **kwargs: Any) -> str:
        """List active subagents."""
        active_tasks = self._manager.progress_manager.list_active_tasks()
        
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
