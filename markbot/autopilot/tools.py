"""Autopilot tools — agent-callable tools for managing the autopilot pipeline.

Design principle: agent tools manage the task lifecycle (intake, list, status,
reject, requeue, verify), but do NOT recursively invoke the agent loop.
Actual task execution is triggered via CLI `markbot autopilot tick`, which
creates a dedicated agent loop session. Within an active agent conversation,
the agent can use `autopilot_pick_next` to get the next task's prompt and
execute it in the current session, then call `autopilot_verify` to run
verification and update the task status.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from markbot.tools.base import BaseTool
from markbot.types.permission import PermissionDecision
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from markbot.autopilot.store import AutopilotStore


_store_cache: dict[Path, AutopilotStore] = {}


def _get_store(workspace: Path) -> AutopilotStore:
    from markbot.autopilot.store import AutopilotStore

    resolved = workspace.resolve()
    if not str(resolved) or str(resolved) == ".":
        raise ValueError("Autopilot requires a valid workspace path")
    if resolved not in _store_cache:
        _store_cache[resolved] = AutopilotStore(resolved)
    return _store_cache[resolved]


def _invalidate_store(workspace: Path) -> None:
    resolved = workspace.resolve()
    _store_cache.pop(resolved, None)


class AutopilotIntakeTool(BaseTool):
    """Submit a new task to the autopilot pipeline."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="autopilot_intake",
            description=(
                "Submit a new task to the autopilot pipeline. "
                "The task will be scored, queued, and can be executed later. "
                "Use this when the user wants to schedule an automated task "
                "that should be independently executed and verified. "
                "For simple step tracking within the current session, "
                "use the `todo` tool instead."
            ),
            parameters=[
                ToolParameter(
                    name="title",
                    type="string",
                    description="Short title describing the task",
                    required=True,
                ),
                ToolParameter(
                    name="body",
                    type="string",
                    description="Detailed description of what the task should accomplish",
                    required=False,
                ),
                ToolParameter(
                    name="source_kind",
                    type="string",
                    description="Origin of the task",
                    required=False,
                    enum=[
                        "manual_idea", "github_issue",
                        "github_pr", "agent_candidate", "cron_trigger",
                    ],
                ),
                ToolParameter(
                    name="source_ref",
                    type="string",
                    description="Reference to the source (e.g., 'issue:42')",
                    required=False,
                ),
                ToolParameter(
                    name="labels",
                    type="array",
                    description="Labels to tag the task with",
                    required=False,
                ),
            ],
            is_read_only=False,
            is_destructive=False,
        )

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        workspace = Path(context.workspace)
        store = _get_store(workspace)
        config = store.load_config()
        if config.autopilot_policy.default_human_gate:
            return PermissionDecision(behavior="ask")
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        workspace = Path(context.workspace)
        store = _get_store(workspace)

        card, created = store.enqueue_card(
            source_kind=params.get("source_kind", "agent_candidate"),
            title=params["title"],
            body=params.get("body", ""),
            source_ref=params.get("source_ref", ""),
            labels=params.get("labels"),
        )
        action = "Created" if created else "Updated"
        return (
            f"{action} autopilot task.\n"
            f"- ID: {card.id}\n"
            f"- Title: {card.title}\n"
            f"- Score: {card.score}\n"
            f"- Status: {card.status}\n"
            f"- Score reasons: {', '.join(card.score_reasons)}\n"
            f"Use `autopilot_list` to see all tasks, "
            f"or `autopilot_pick_next` to pick the next task for execution."
        )


class AutopilotListTool(BaseTool):
    """List autopilot tasks."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="autopilot_list",
            description="List all autopilot tasks, optionally filtered by status.",
            parameters=[
                ToolParameter(
                    name="status",
                    type="string",
                    description="Filter by task status",
                    required=False,
                    enum=[
                        "queued", "accepted", "preparing", "running",
                        "verifying", "repairing", "completed", "failed",
                        "rejected", "superseded",
                    ],
                ),
            ],
            is_read_only=True,
            is_destructive=False,
        )

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        from markbot.autopilot.store import _shorten

        workspace = Path(context.workspace)
        store = _get_store(workspace)

        status = params.get("status")
        cards = store.list_cards(status=status)
        stats = store.stats()

        if not cards:
            return "No autopilot tasks found."

        lines = ["## Autopilot Tasks", ""]
        lines.append(f"**Stats**: {_json_stats(stats)}")
        lines.append("")
        for card in cards[:20]:
            lines.append(
                f"- [{card.status}] **{card.id}** {card.title} "
                f"(score={card.score}, source={card.source_kind})"
            )
            if card.body:
                lines.append(f"  > {_shorten(card.body, limit=100)}")
        accepted = [c for c in cards if c.status == "accepted"]
        if accepted:
            lines.append("")
            lines.append(
                f"⚠ {len(accepted)} task(s) in 'accepted' state — "
                f"use `autopilot_pick_next` to work on them, "
                f"or `autopilot_reject`/`autopilot_requeue` to release."
            )
        return "\n".join(lines)


class AutopilotPickNextTool(BaseTool):
    """Pick the next queued task and prepare it for execution."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="autopilot_pick_next",
            description=(
                "Pick the highest-scored queued autopilot task and return its execution prompt. "
                "This does NOT execute the task — it returns the task details and a prompt that "
                "you can use to work on the task in the current session. "
                "After completing the work, call `autopilot_verify` "
                "to run verification and update the task status."
            ),
            parameters=[],
            is_read_only=True,
            is_destructive=False,
        )

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        from markbot.autopilot.service import _build_execution_prompt

        workspace = Path(context.workspace)
        store = _get_store(workspace)

        card = store.pick_next_card()
        if card is None:
            return "No queued tasks available for execution."

        store.update_status(card.id, status="accepted", note="picked by agent")

        config = store.load_config()
        prompt = _build_execution_prompt(card, config)

        return (
            f"## Next Task Picked\n\n"
            f"- **ID**: {card.id}\n"
            f"- **Title**: {card.title}\n"
            f"- **Score**: {card.score}\n"
            f"- **Source**: {card.source_kind}\n"
            f"- **Status**: accepted (ready for execution)\n\n"
            f"## Execution Prompt\n\n{prompt}\n\n"
            f"Work on this task now. When done, call `autopilot_verify` with task_id=`{card.id}` "
            f"to run verification and update the task status."
        )


class AutopilotVerifyTool(BaseTool):
    """Run verification for a task and update its status."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="autopilot_verify",
            description=(
                "Run verification commands for an autopilot task and update its status. "
                "Call this after you have completed work on a task. "
                "If verification passes, the task is marked as completed. "
                "If verification fails, the task is marked for repair."
            ),
            parameters=[
                ToolParameter(
                    name="task_id",
                    type="string",
                    description="The autopilot task ID to verify",
                    required=True,
                ),
                ToolParameter(
                    name="summary",
                    type="string",
                    description="Summary of what was done for this task",
                    required=False,
                ),
            ],
            is_read_only=False,
            is_destructive=False,
        )

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        workspace = Path(context.workspace)
        store = _get_store(workspace)
        config = store.load_config()
        if config.autopilot_policy.default_human_gate:
            return PermissionDecision(behavior="ask")
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        from markbot.autopilot.verification import (
            render_verification_report,
            run_verification_steps,
            verification_passed,
        )

        workspace = Path(context.workspace)
        store = _get_store(workspace)
        task_id = params["task_id"]
        summary = params.get("summary", "")

        card = store.get_card(task_id)
        if card is None:
            return f"Task '{task_id}' not found."

        config = store.load_config()
        store.update_status(task_id, status="verifying", note="running verification")

        steps = run_verification_steps(
            config.verification_policy,
            cwd=workspace,
        )

        attempt = card.metadata.get("attempt_count", 0) + 1
        run_dir = store.runs_dir / task_id / f"attempt-{attempt}"
        run_dir.mkdir(parents=True, exist_ok=True)

        v_report = run_dir / "verification_report.md"
        v_report.write_text(
            render_verification_report(card.title, card.id, steps),
            encoding="utf-8",
        )

        if verification_passed(steps):
            store.update_status(
                task_id,
                status="completed",
                note="verification passed",
                metadata_updates={
                    "verification_failed": False,
                    "agent_summary": summary,
                    "attempt_count": attempt,
                },
            )
            result_text = "PASSED"
        else:
            store.update_status(
                task_id,
                status="repairing",
                note="verification failed, needs repair",
                metadata_updates={
                    "verification_failed": True,
                    "agent_summary": summary,
                    "attempt_count": attempt,
                },
            )
            result_text = "FAILED"

        lines = [
            f"## Verification Result: {result_text}",
            "",
            f"- **Task ID**: {task_id}",
            f"- **Title**: {card.title}",
        ]
        if steps:
            passed_count = sum(1 for s in steps if s.status == "success")
            lines.append(f"- **Steps**: {passed_count}/{len(steps)} passed")
            for step in steps:
                icon = "✓" if step.status == "success" else "✗"
                lines.append(f"  - {icon} `{step.command}` (rc={step.returncode})")
                if step.status != "success" and step.stderr:
                    lines.append(f"    stderr: {step.stderr[:200]}")
        else:
            lines.append("- No verification commands configured.")
        lines.append(f"- **Report**: {v_report}")

        if result_text == "FAILED":
            lines.append("\nYou can call `autopilot_requeue` to retry this task later.")

        return "\n".join(lines)


class AutopilotStatusTool(BaseTool):
    """Get detailed status of a specific autopilot task."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="autopilot_status",
            description="Get detailed status of a specific autopilot task by its ID.",
            parameters=[
                ToolParameter(
                    name="task_id",
                    type="string",
                    description="The autopilot task ID (e.g., 'ap-abc12345')",
                    required=True,
                ),
            ],
            is_read_only=True,
            is_destructive=False,
        )

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        workspace = Path(context.workspace)
        store = _get_store(workspace)

        card = store.get_card(params["task_id"])
        if card is None:
            return f"Task '{params['task_id']}' not found."

        lines = [
            f"## Task: {card.id}",
            "",
            f"- **Title**: {card.title}",
            f"- **Status**: {card.status}",
            f"- **Source**: {card.source_kind} ({card.source_ref or 'N/A'})",
            f"- **Score**: {card.score}",
            f"- **Score reasons**: {', '.join(card.score_reasons)}",
            f"- **Labels**: {', '.join(card.labels) if card.labels else 'none'}",
        ]
        if card.body:
            lines.append(f"- **Body**: {card.body}")
        last_note = card.metadata.get("last_note")
        if last_note:
            lines.append(f"- **Last note**: {last_note}")
        return "\n".join(lines)


class AutopilotRejectTool(BaseTool):
    """Reject an autopilot task."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="autopilot_reject",
            description="Reject an autopilot task, removing it from the queue.",
            parameters=[
                ToolParameter(
                    name="task_id",
                    type="string",
                    description="The autopilot task ID to reject",
                    required=True,
                ),
                ToolParameter(
                    name="reason",
                    type="string",
                    description="Reason for rejection",
                    required=False,
                ),
            ],
            is_read_only=False,
            is_destructive=True,
        )

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        workspace = Path(context.workspace)
        store = _get_store(workspace)
        config = store.load_config()
        if config.autopilot_policy.default_human_gate:
            return PermissionDecision(behavior="ask")
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        workspace = Path(context.workspace)
        store = _get_store(workspace)
        task_id = params["task_id"]

        card = store.get_card(task_id)
        if card is None:
            return f"Task '{task_id}' not found."

        rejectable = ("queued", "accepted", "repairing", "failed")
        if card.status not in rejectable:
            return (
                f"Cannot reject task {card.id}: status is '{card.status}'. "
                f"Only tasks in {rejectable} can be rejected."
            )

        try:
            reason = params.get("reason", "")
            card = store.update_status(
                task_id, status="rejected", note=reason,
            )
            return f"Task {card.id} rejected."
        except ValueError as exc:
            return str(exc)


class AutopilotRequeueTool(BaseTool):
    """Requeue a previously failed or rejected task."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="autopilot_requeue",
            description=(
                "Requeue an autopilot task for another attempt. "
                "Works on tasks in 'failed', 'rejected', or 'repairing' status."
            ),
            parameters=[
                ToolParameter(
                    name="task_id",
                    type="string",
                    description="The autopilot task ID to requeue",
                    required=True,
                ),
            ],
            is_read_only=False,
            is_destructive=False,
        )

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        workspace = Path(context.workspace)
        store = _get_store(workspace)
        config = store.load_config()
        if config.autopilot_policy.default_human_gate:
            return PermissionDecision(behavior="ask")
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        workspace = Path(context.workspace)
        store = _get_store(workspace)
        task_id = params["task_id"]

        card = store.get_card(task_id)
        if card is None:
            return f"Task '{task_id}' not found."

        requeueable = ("failed", "rejected", "repairing", "accepted")
        if card.status not in requeueable:
            return (
                f"Cannot requeue task {card.id}: status is '{card.status}'. "
                f"Only tasks in {requeueable} can be requeued."
            )

        try:
            card = store.update_status(task_id, status="queued", note="requeued by agent")
            return f"Task {card.id} requeued with score {card.score}."
        except ValueError as exc:
            return str(exc)


def _json_stats(stats: dict[str, int]) -> str:
    parts = [f"{k}={v}" for k, v in sorted(stats.items())]
    return ", ".join(parts) if parts else "no tasks"


ALL_AUTOPILOT_TOOLS = [
    AutopilotIntakeTool,
    AutopilotListTool,
    AutopilotPickNextTool,
    AutopilotVerifyTool,
    AutopilotStatusTool,
    AutopilotRejectTool,
    AutopilotRequeueTool,
]
