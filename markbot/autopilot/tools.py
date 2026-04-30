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
from typing import Any

from markbot.tools.base import Tool
from markbot.types.tool import ToolContext


class AutopilotIntakeTool(Tool):
    """Submit a new task to the autopilot pipeline."""

    _is_read_only = False

    @property
    def name(self) -> str:
        return "autopilot_intake"

    @property
    def description(self) -> str:
        return (
            "Submit a new task to the autopilot pipeline. "
            "The task will be scored, queued, and can be executed later. "
            "Use this when the user wants to schedule an automated task "
            "that should be independently executed and verified. "
            "For simple step tracking within the current session, "
            "use the `todo` tool instead."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short title describing the task",
                },
                "body": {
                    "type": "string",
                    "description": "Detailed description of what the task should accomplish",
                },
                "source_kind": {
                    "type": "string",
                    "description": "Origin of the task",
                    "enum": [
                        "manual_idea", "github_issue",
                        "github_pr", "agent_candidate", "cron_trigger",
                    ],
                },
                "source_ref": {
                    "type": "string",
                    "description": "Reference to the source (e.g., 'issue:42')",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels to tag the task with",
                },
            },
            "required": ["title"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        from markbot.autopilot.store import AutopilotStore

        context: ToolContext = kwargs.pop("_tool_context")
        workspace = Path(context.workspace)
        store = AutopilotStore(workspace)

        card, created = store.enqueue_card(
            source_kind=kwargs.get("source_kind", "agent_candidate"),
            title=kwargs["title"],
            body=kwargs.get("body", ""),
            source_ref=kwargs.get("source_ref", ""),
            labels=kwargs.get("labels"),
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


class AutopilotListTool(Tool):
    """List autopilot tasks."""

    _is_read_only = True

    @property
    def name(self) -> str:
        return "autopilot_list"

    @property
    def description(self) -> str:
        return "List all autopilot tasks, optionally filtered by status."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by task status",
                    "enum": [
                        "queued", "accepted", "preparing", "running",
                        "verifying", "repairing", "completed", "failed",
                        "rejected", "superseded",
                    ],
                },
            },
        }

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        from markbot.autopilot.store import AutopilotStore

        context: ToolContext = kwargs.pop("_tool_context")
        workspace = Path(context.workspace)
        store = AutopilotStore(workspace)

        status = kwargs.get("status")
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
                from markbot.autopilot.store import _shorten
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


class AutopilotPickNextTool(Tool):
    """Pick the next queued task and prepare it for execution."""

    _is_read_only = True

    @property
    def name(self) -> str:
        return "autopilot_pick_next"

    @property
    def description(self) -> str:
        return (
            "Pick the highest-scored queued autopilot task and return its execution prompt. "
            "This does NOT execute the task — it returns the task details and a prompt that "
            "you can use to work on the task in the current session. "
            "After completing the work, call `autopilot_verify` "
            "to run verification and update the task status."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        from markbot.autopilot.store import AutopilotStore

        context: ToolContext = kwargs.pop("_tool_context")
        workspace = Path(context.workspace)
        store = AutopilotStore(workspace)

        card = store.pick_next_card()
        if card is None:
            return "No queued tasks available for execution."

        store.update_status(card.id, status="accepted", note="picked by agent")

        config = store.load_config()
        from markbot.autopilot.service import _build_execution_prompt
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


class AutopilotVerifyTool(Tool):
    """Run verification for a task and update its status."""

    _is_read_only = False

    @property
    def name(self) -> str:
        return "autopilot_verify"

    @property
    def description(self) -> str:
        return (
            "Run verification commands for an autopilot task and update its status. "
            "Call this after you have completed work on a task. "
            "If verification passes, the task is marked as completed. "
            "If verification fails, the task is marked for repair."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The autopilot task ID to verify",
                },
                "summary": {
                    "type": "string",
                    "description": "Summary of what was done for this task",
                },
            },
            "required": ["task_id"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        from markbot.autopilot.store import AutopilotStore
        from markbot.autopilot.verification import (
            render_verification_report,
            run_verification_steps,
            verification_passed,
        )

        context: ToolContext = kwargs.pop("_tool_context")
        workspace = Path(context.workspace)
        store = AutopilotStore(workspace)
        task_id = kwargs["task_id"]
        summary = kwargs.get("summary", "")

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


class AutopilotStatusTool(Tool):
    """Get detailed status of a specific autopilot task."""

    _is_read_only = True

    @property
    def name(self) -> str:
        return "autopilot_status"

    @property
    def description(self) -> str:
        return "Get detailed status of a specific autopilot task by its ID."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The autopilot task ID (e.g., 'ap-abc12345')",
                },
            },
            "required": ["task_id"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        from markbot.autopilot.store import AutopilotStore

        context: ToolContext = kwargs.pop("_tool_context")
        workspace = Path(context.workspace)
        store = AutopilotStore(workspace)

        card = store.get_card(kwargs["task_id"])
        if card is None:
            return f"Task '{kwargs['task_id']}' not found."

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


class AutopilotRejectTool(Tool):
    """Reject an autopilot task."""

    _is_read_only = False

    @property
    def name(self) -> str:
        return "autopilot_reject"

    @property
    def description(self) -> str:
        return "Reject an autopilot task, removing it from the queue."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The autopilot task ID to reject",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for rejection",
                },
            },
            "required": ["task_id"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        from markbot.autopilot.store import AutopilotStore

        context: ToolContext = kwargs.pop("_tool_context")
        workspace = Path(context.workspace)
        store = AutopilotStore(workspace)
        task_id = kwargs["task_id"]

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
            reason = kwargs.get("reason", "")
            card = store.update_status(
                task_id, status="rejected", note=reason,
            )
            return f"Task {card.id} rejected."
        except ValueError as exc:
            return str(exc)


class AutopilotRequeueTool(Tool):
    """Requeue a previously failed or rejected task."""

    _is_read_only = False

    @property
    def name(self) -> str:
        return "autopilot_requeue"

    @property
    def description(self) -> str:
        return (
            "Requeue an autopilot task for another attempt. "
            "Works on tasks in 'failed', 'rejected', or 'repairing' status."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The autopilot task ID to requeue",
                },
            },
            "required": ["task_id"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        from markbot.autopilot.store import AutopilotStore

        context: ToolContext = kwargs.pop("_tool_context")
        workspace = Path(context.workspace)
        store = AutopilotStore(workspace)
        task_id = kwargs["task_id"]

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
