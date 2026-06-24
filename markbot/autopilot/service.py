"""Autopilot Service — core orchestration logic for the automation pipeline.

Pipeline: Intake → Score → Accept → Execute → Verify → Repair → Complete/Fail

Adapted from OpenHarness's RepoAutopilotService, but integrated with MarkBot's
AgentLoop.process_direct() for task execution instead of OpenHarness's build_runtime().
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from markbot.autopilot.store import AutopilotStore
from markbot.autopilot.types import (
    AutopilotConfig,
    TaskCard,
    TaskRunResult,
    TaskStatus,
    VerificationStep,
)
from markbot.autopilot.verification import (
    render_verification_report,
    run_verification_steps,
    verification_passed,
)

if TYPE_CHECKING:
    from markbot.agent.loop import AgentLoop


def _shorten(text: str, *, limit: int = 120) -> str:
    from markbot.utils.helpers import shorten
    return shorten(text, limit=limit)


def _build_execution_prompt(card: TaskCard, config: AutopilotConfig) -> str:
    autopilot_policy = json.dumps(asdict(config.autopilot_policy), indent=2)
    verification_policy = json.dumps(asdict(config.verification_policy), indent=2)
    return (
        "You are executing one autopilot task for the current workspace.\n\n"
        "Goal:\n"
        "- Make the smallest coherent implementation that resolves the task.\n"
        "- Run the relevant verification commands yourself before stopping.\n"
        "- Do not perform irreversible external actions.\n"
        "- Leave the workspace in a reviewable state and summarize what changed.\n\n"
        f"Task ID: {card.id}\n"
        f"Source: {card.source_kind}\n"
        f"Source ref: {card.source_ref or '-'}\n"
        f"Title: {card.title}\n"
        f"Body:\n{card.body or '(none)'}\n\n"
        "Autopilot policy:\n"
        f"{autopilot_policy}\n\n"
        "Verification policy:\n"
        f"{verification_policy}\n\n"
        "Expected output:\n"
        "1. What you changed.\n"
        "2. What you verified.\n"
        "3. Any remaining risk or human follow-up.\n"
    )


def _render_run_report(
    card: TaskCard,
    *,
    agent_summary: str,
    verification_steps: list[VerificationStep],
    verification_status: str,
) -> str:
    lines = [
        f"# Autopilot Run Report: {card.id}",
        "",
        f"Title: {card.title}",
        f"Source: {card.source_kind}",
        f"Source ref: {card.source_ref or '-'}",
        "",
        "## Agent Self-Reported Summary",
        "",
        agent_summary.strip() or "(empty agent summary)",
        "",
        "## Service-Level Verification",
        "",
    ]
    if verification_status == "not_started":
        lines.extend([
            "- Verification status: not started.",
            "- The agent run failed before verification could begin.",
        ])
    elif verification_status == "passed":
        lines.append("- Verification status: PASSED.")
        for step in verification_steps:
            lines.append(f"  - [{step.status}] `{step.command}` (rc={step.returncode})")
    elif verification_status == "failed":
        lines.append("- Verification status: FAILED.")
        for step in verification_steps:
            lines.append(f"  - [{step.status}] `{step.command}` (rc={step.returncode})")
            if step.stderr:
                lines.append(f"    stderr: {_shorten(step.stderr, limit=200)}")
    else:
        lines.append(f"- Verification status: {verification_status}.")
    return "\n".join(lines).strip() + "\n"


class AutopilotService:
    """Orchestrate the autopilot pipeline within a MarkBot workspace."""

    def __init__(self, store: AutopilotStore, agent_loop: "AgentLoop") -> None:
        self._store = store
        self._agent_loop = agent_loop

    @property
    def store(self) -> AutopilotStore:
        return self._store

    @property
    def agent_loop(self) -> "AgentLoop":
        return self._agent_loop

    async def intake(
        self,
        *,
        source_kind: str,
        title: str,
        body: str = "",
        source_ref: str = "",
        labels: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskCard:
        card, created = self._store.enqueue_card(
            source_kind=source_kind,
            title=title,
            body=body,
            source_ref=source_ref,
            labels=labels,
            metadata=metadata,
        )
        action = "queued" if created else "refreshed"
        logger.info("Autopilot intake {}: {} - {}", action, card.id, card.title)
        return card

    async def run_next(
        self,
        *,
        model: str | None = None,
        max_turns: int | None = None,
    ) -> TaskRunResult | None:
        card = self._store.pick_next_card()
        if card is None:
            logger.info("Autopilot: no queued tasks to run")
            return None

        config = self._store.load_config()
        effective_max_turns = max_turns or config.autopilot_policy.max_turns
        effective_model = model or config.autopilot_policy.default_model or None
        max_attempts = config.autopilot_policy.max_attempts
        repair_max_rounds = config.autopilot_policy.repair_max_rounds

        self._store.update_status(card.id, status="accepted")
        return await self._execute_card(
            card=card,
            config=config,
            model=effective_model,
            max_turns=effective_max_turns,
            max_attempts=max(max_attempts, repair_max_rounds + 1, 1),
        )

    async def _execute_card(
        self,
        *,
        card: TaskCard,
        config: AutopilotConfig,
        model: str | None,
        max_turns: int,
        max_attempts: int,
    ) -> TaskRunResult:
        attempt_count = 0
        prior_failure_stage: str | None = None
        prior_failure_summary: str | None = None
        current_run_report = Path("")
        current_verification_report = Path("")
        verification_steps: list[VerificationStep] = []
        assistant_summary = ""
        working_cwd = self._store.workspace

        while attempt_count < max_attempts:
            attempt_count += 1
            is_repair = attempt_count > 1

            self._store.update_status(
                card.id,
                status="repairing" if is_repair else "preparing",
                note=f"attempt {attempt_count}/{max_attempts}",
            )

            prompt = _build_execution_prompt(card, config)
            if is_repair and prior_failure_stage:
                prompt += (
                    f"\n\n## Repair Context\n"
                    f"Previous attempt failed at stage: {prior_failure_stage}\n"
                    f"Failure summary: {prior_failure_summary or 'N/A'}\n"
                    f"Attempt: {attempt_count}/{max_attempts}\n"
                    f"Please fix the issue and try again.\n"
                )

            self._store.update_status(card.id, status="running")
            self._store.append_journal(
                kind="run_start",
                summary=f"{card.title}: attempt {attempt_count} started",
                task_id=card.id,
                metadata={"attempt": attempt_count, "is_repair": is_repair},
            )

            try:
                agent_loop = self._agent_loop
                response = await agent_loop.process_direct(
                    prompt,
                    session_key=f"autopilot:{card.id}",
                    channel="autopilot",
                    chat_id=card.id,
                )
                assistant_summary = response.content if response else ""
            except Exception as exc:
                logger.error("Autopilot agent execution failed: {}", exc)
                assistant_summary = ""
                self._store.update_status(
                    card.id,
                    status="failed",
                    note=f"agent runtime error: {exc}",
                    metadata_updates={
                        "last_failure_stage": "agent_runtime_error",
                        "last_failure_summary": str(exc),
                    },
                )
                return TaskRunResult(
                    card_id=card.id,
                    status="failed",
                    assistant_summary=str(exc),
                    attempt_count=attempt_count,
                    worktree_path=str(working_cwd),
                )

            self._store.update_status(card.id, status="verifying")

            verification_steps = run_verification_steps(
                config.verification_policy,
                cwd=working_cwd,
            )

            run_dir = self._store.runs_dir / card.id / f"attempt-{attempt_count}"
            run_dir.mkdir(parents=True, exist_ok=True)

            current_run_report = run_dir / "run_report.md"
            current_verification_report = run_dir / "verification_report.md"

            v_status = "passed" if verification_passed(verification_steps) else "failed"
            current_run_report.write_text(
                _render_run_report(
                    card,
                    agent_summary=assistant_summary,
                    verification_steps=verification_steps,
                    verification_status=v_status,
                ),
                encoding="utf-8",
            )
            current_verification_report.write_text(
                render_verification_report(card.title, card.id, verification_steps),
                encoding="utf-8",
            )

            if verification_passed(verification_steps):
                self._store.update_status(
                    card.id,
                    status="completed",
                    note=f"verification passed on attempt {attempt_count}",
                    metadata_updates={
                        "verification_failed": False,
                        "attempt_count": attempt_count,
                    },
                )
                self._store.append_journal(
                    kind="completed",
                    summary=f"{card.title}: verification passed on attempt {attempt_count}",
                    task_id=card.id,
                    metadata={"attempt": attempt_count},
                )
                return TaskRunResult(
                    card_id=card.id,
                    status="completed",
                    assistant_summary=assistant_summary,
                    run_report_path=str(current_run_report),
                    verification_report_path=str(current_verification_report),
                    verification_steps=verification_steps,
                    attempt_count=attempt_count,
                    worktree_path=str(working_cwd),
                )

            prior_failure_stage = "local_verification_failed"
            failed_cmds = [s.command for s in verification_steps if s.status == "failed"]
            prior_failure_summary = "; ".join(failed_cmds) if failed_cmds else "verification failed"

            if attempt_count < max_attempts:
                repair_retry_on = config.autopilot_policy.repair_retry_on
                repair_stop_on = config.autopilot_policy.repair_stop_on

                should_retry = prior_failure_stage in repair_retry_on or not repair_retry_on
                should_stop = prior_failure_stage in repair_stop_on

                if should_stop:
                    logger.info(
                        "Autopilot: stopping repairs for {} due to {}",
                        card.id, prior_failure_stage,
                    )
                    break

                if not should_retry:
                    logger.info(
                        "Autopilot: not retrying {} for failure type {}",
                        card.id, prior_failure_stage,
                    )
                    break

                self._store.append_journal(
                    kind="verification_failed_retry",
                    summary=f"{card.title}: verification failed, retrying",
                    task_id=card.id,
                    metadata={"attempt": attempt_count, "failed_commands": failed_cmds},
                )
                continue

        self._store.update_status(
            card.id,
            status="failed",
            note="repair rounds exhausted",
            metadata_updates={
                "last_failure_stage": prior_failure_stage or "repair_exhausted",
                "last_failure_summary": prior_failure_summary or "repair rounds exhausted",
            },
        )
        self._store.append_journal(
            kind="failed",
            summary=f"{card.title}: repair rounds exhausted",
            task_id=card.id,
            metadata={"attempt": attempt_count},
        )
        return TaskRunResult(
            card_id=card.id,
            status="failed",
            assistant_summary=assistant_summary,
            run_report_path=str(current_run_report),
            verification_report_path=str(current_verification_report),
            verification_steps=verification_steps,
            attempt_count=attempt_count,
            worktree_path=str(working_cwd),
        )

    async def tick(
        self,
        *,
        model: str | None = None,
        max_turns: int | None = None,
    ) -> TaskRunResult | None:
        active_statuses: set[TaskStatus] = {
            "preparing", "running", "verifying", "repairing",
        }
        if any(c.status in active_statuses for c in self._store.list_cards()):
            self._store.append_journal(
                kind="tick_skip",
                summary="Skipped run-next because another card is active",
            )
            return None
        if self._store.pick_next_card() is None:
            self._store.append_journal(
                kind="tick_idle",
                summary="Tick completed with no queued work",
            )
            return None
        return await self.run_next(model=model, max_turns=max_turns)

    def list_tasks(self, *, status: TaskStatus | None = None) -> list[TaskCard]:
        return self._store.list_cards(status=status)

    def get_task(self, card_id: str) -> TaskCard | None:
        return self._store.get_card(card_id)

    def get_stats(self) -> dict[str, int]:
        return self._store.stats()

    def get_active_context(self) -> str:
        return self._store.load_active_context()

    def reject_task(self, card_id: str, *, reason: str = "") -> TaskCard:
        return self._store.update_status(
            card_id,
            status="rejected",
            note=reason or "rejected by user",
        )

    def requeue_task(self, card_id: str) -> TaskCard:
        return self._store.update_status(
            card_id,
            status="queued",
            note="requeued by user",
        )
