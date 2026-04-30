"""Autopilot data models — adapted from OpenHarness for MarkBot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TaskStatus = Literal[
    "queued",
    "accepted",
    "preparing",
    "running",
    "verifying",
    "repairing",
    "completed",
    "failed",
    "rejected",
    "superseded",
]

TaskSource = Literal[
    "manual_idea",
    "github_issue",
    "github_pr",
    "agent_candidate",
    "cron_trigger",
]


@dataclass
class TaskCard:
    id: str
    fingerprint: str
    title: str
    body: str = ""
    source_kind: TaskSource = "manual_idea"
    source_ref: str = ""
    status: TaskStatus = "queued"
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class TaskJournalEntry:
    timestamp: float
    kind: str
    summary: str
    task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskRegistry:
    version: int = 1
    updated_at: float = 0.0
    cards: list[TaskCard] = field(default_factory=list)


@dataclass
class VerificationStep:
    command: str
    returncode: int
    status: Literal["success", "failed", "skipped", "error"]
    stdout: str = ""
    stderr: str = ""


@dataclass
class TaskRunResult:
    card_id: str
    status: TaskStatus
    assistant_summary: str = ""
    run_report_path: str = ""
    verification_report_path: str = ""
    verification_steps: list[VerificationStep] = field(default_factory=list)
    attempt_count: int = 0
    worktree_path: str = ""


@dataclass
class VerificationCommand:
    raw: str
    argv: tuple[str, ...] = ()
    shell: bool = False
    error: str | None = None


@dataclass
class AutopilotPolicy:
    intake_max_visible: int = 12
    default_human_gate: bool = True
    prefer_small_safe_steps: bool = True
    default_model: str = ""
    max_turns: int = 12
    max_attempts: int = 3
    repair_max_rounds: int = 2
    repair_retry_on: list[str] = field(
        default_factory=lambda: ["local_verification_failed"],
    )
    repair_stop_on: list[str] = field(
        default_factory=lambda: ["agent_runtime_error", "permission_error"],
    )


@dataclass
class VerificationPolicy:
    commands: list[str] = field(default_factory=list)
    require_tests_before_complete: bool = True


@dataclass
class AutopilotConfig:
    autopilot_policy: AutopilotPolicy = field(default_factory=AutopilotPolicy)
    verification_policy: VerificationPolicy = field(default_factory=VerificationPolicy)
