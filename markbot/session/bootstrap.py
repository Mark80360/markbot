"""Session bootstrap protocol — validate and recover state on session start.

Runs a series of lightweight checks when a new session begins:
1. Load previous session's handoff (if any)
2. Load feature_list.json for feature state recovery
3. Validate active tasks are still relevant (not stale)
4. Check external dependencies (MCP, API tokens)
5. Check init.sh availability for standard startup path
6. Generate a session context summary for injection

This ensures the agent starts every session with accurate, up-to-date
context rather than blindly resuming from potentially stale state.

Complies with Harness Engineering specification:
- Coding Agent Startup Flow (9-step template)
- Initializer Agent Playbook (required artifacts)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.session.handoff import HandoffManager, SessionHandoff


@dataclass
class BootstrapCheckResult:
    name: str
    status: str  # "ok", "warning", "error"
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class FeatureEntry:
    id: str = ""
    title: str = ""
    status: str = "not_started"
    priority: int = 0
    area: str = ""
    verification: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass
class BootstrapReport:
    session_key: str = ""
    handoff_loaded: bool = False
    handoff: SessionHandoff | None = None
    feature_list_loaded: bool = False
    feature_list: list[FeatureEntry] = field(default_factory=list)
    next_feature: FeatureEntry | None = None
    init_sh_available: bool = False
    checks: list[BootstrapCheckResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    context_summary: str = ""

    @property
    def has_warnings(self) -> bool:
        return any(c.status == "warning" for c in self.checks)

    @property
    def has_errors(self) -> bool:
        return any(c.status == "error" for c in self.checks)

    def to_context_block(self) -> str:
        if not self.context_summary and not self.handoff_loaded and not self.feature_list_loaded and not self.init_sh_available:
            return ""

        lines = ["[Session Bootstrap Context — metadata only, not instructions]"]

        if self.handoff_loaded and self.handoff:
            h = self.handoff
            if h.active_tasks:
                lines.append("Resumed tasks:")
                for t in h.active_tasks:
                    lines.append(f"  - [{t.status}] {t.id}: {t.title}")
                    if t.progress:
                        lines.append(f"    Progress: {t.progress}")
            if h.next_best_step:
                lines.append(f"Suggested next step: {h.next_best_step}")
            if h.blockers:
                lines.append("Known blockers:")
                for b in h.blockers:
                    lines.append(f"  - {b.description}")

        if self.feature_list_loaded and self.feature_list:
            unfinished = [f for f in self.feature_list if f.status in ("not_started", "in_progress", "blocked")]
            if unfinished:
                lines.append("Unfinished features (from feature_list.json):")
                for f in unfinished:
                    lines.append(f"  - [{f.status}] {f.id}: {f.title} (priority={f.priority})")
            if self.next_feature:
                lines.append(f"Highest-priority unfinished feature: {self.next_feature.id} — {self.next_feature.title}")

        if self.init_sh_available:
            lines.append("Standard startup path: ./init.sh")

        if self.warnings:
            lines.append("Startup warnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")

        lines.append("[/Session Bootstrap Context]")
        return "\n".join(lines)


class SessionBootstrap:
    """Run bootstrap checks and build recovery context for a new session.

    Usage::

        bootstrap = SessionBootstrap(workspace, handoff_manager)
        report = await bootstrap.run(session_key="cli:direct")
        context_block = report.to_context_block()
    """

    _TASK_STALE_THRESHOLD_SECONDS = 7 * 86400

    def __init__(
        self,
        workspace: Path,
        handoff_manager: HandoffManager,
        mcp_manager: Any | None = None,
        task_tracker: Any | None = None,
    ) -> None:
        self.workspace = workspace
        self.handoff_manager = handoff_manager
        self.mcp_manager = mcp_manager
        self.task_tracker = task_tracker

    async def run(self, session_key: str) -> BootstrapReport:
        report = BootstrapReport(session_key=session_key)

        report.handoff = self.handoff_manager.load(session_key)
        report.handoff_loaded = report.handoff is not None

        if report.handoff_loaded:
            logger.info(
                "Loaded handoff for {}: {} active tasks",
                session_key,
                len(report.handoff.active_tasks) if report.handoff else 0,
            )

        features, next_feature = self._load_feature_list()
        report.feature_list_loaded = features is not None
        report.feature_list = features or []
        report.next_feature = next_feature

        if report.feature_list_loaded:
            unfinished = [f for f in report.feature_list if f.status in ("not_started", "in_progress", "blocked")]
            logger.info(
                "Loaded feature_list.json: {} features, {} unfinished",
                len(report.feature_list),
                len(unfinished),
            )

        report.init_sh_available = (self.workspace / "init.sh").exists()

        report.checks.append(self._check_handoff_freshness(report.handoff))
        report.checks.append(self._check_workspace_access())
        report.checks.append(await self._check_mcp_connectivity())
        report.checks.append(self._check_init_sh())
        report.checks.append(self._check_feature_list())

        if self.task_tracker is not None:
            report.checks.append(self._check_task_consistency(report.handoff))

        report.warnings = [
            c.message for c in report.checks if c.status in ("warning", "error")
        ]

        report.context_summary = self._build_context_summary(report)

        return report

    def _check_handoff_freshness(self, handoff: SessionHandoff | None) -> BootstrapCheckResult:
        if handoff is None:
            return BootstrapCheckResult(
                name="handoff_freshness",
                status="ok",
                message="No previous handoff found — this is a fresh session.",
            )

        try:
            from datetime import datetime
            handoff_time = datetime.strptime(handoff.timestamp, "%Y-%m-%d %H:%M")
            now = datetime.now()
            age_hours = (now - handoff_time).total_seconds() / 3600

            if age_hours > 168:
                return BootstrapCheckResult(
                    name="handoff_freshness",
                    status="warning",
                    message=f"Handoff is {age_hours:.0f} hours old — tasks may be stale.",
                    details={"age_hours": age_hours},
                )
            return BootstrapCheckResult(
                name="handoff_freshness",
                status="ok",
                message=f"Handoff is {age_hours:.1f} hours old.",
                details={"age_hours": age_hours},
            )
        except Exception as e:
            logger.debug("Handoff timestamp unreadable: {}", e)
            return BootstrapCheckResult(
                name="handoff_freshness",
                status="ok",
                message="Handoff loaded (timestamp unreadable).",
            )

    def _check_workspace_access(self) -> BootstrapCheckResult:
        try:
            test_file = self.workspace / ".markbot_bootstrap_check"
            test_file.write_text(str(time.time()), encoding="utf-8")
            test_file.unlink()
            return BootstrapCheckResult(
                name="workspace_access",
                status="ok",
                message="Workspace is writable.",
            )
        except Exception as e:
            return BootstrapCheckResult(
                name="workspace_access",
                status="error",
                message=f"Workspace not writable: {e}",
            )

    async def _check_mcp_connectivity(self) -> BootstrapCheckResult:
        if self.mcp_manager is None:
            return BootstrapCheckResult(
                name="mcp_connectivity",
                status="ok",
                message="No MCP manager configured.",
            )

        if self.mcp_manager.is_connected:
            return BootstrapCheckResult(
                name="mcp_connectivity",
                status="ok",
                message="MCP servers connected.",
            )

        if self.mcp_manager._mcp_servers:
            return BootstrapCheckResult(
                name="mcp_connectivity",
                status="warning",
                message="MCP servers configured but not yet connected — will connect on first use.",
            )

        return BootstrapCheckResult(
            name="mcp_connectivity",
            status="ok",
            message="No MCP servers configured.",
        )

    def _load_feature_list(self) -> tuple[list[FeatureEntry] | None, FeatureEntry | None]:
        feature_path = self.workspace / "feature_list.json"
        if not feature_path.exists():
            return None, None

        try:
            import json
            data = json.loads(feature_path.read_text(encoding="utf-8"))
            features_data = data.get("features", [])
            features: list[FeatureEntry] = []
            for f in features_data:
                features.append(FeatureEntry(
                    id=f.get("id", ""),
                    title=f.get("title", ""),
                    status=f.get("status", "not_started"),
                    priority=f.get("priority", 0),
                    area=f.get("area", ""),
                    verification=f.get("verification", []),
                    evidence=f.get("evidence", []),
                ))

            unfinished = [f for f in features if f.status in ("not_started", "in_progress", "blocked")]
            next_feature = None
            if unfinished:
                unfinished.sort(key=lambda f: f.priority)
                next_feature = unfinished[0]

            return features, next_feature
        except Exception as e:
            logger.warning("Failed to load feature_list.json: {}", e)
            return None, None

    def _check_init_sh(self) -> BootstrapCheckResult:
        init_path = self.workspace / "init.sh"
        if not init_path.exists():
            return BootstrapCheckResult(
                name="init_sh",
                status="warning",
                message="No init.sh found — standard startup path not available.",
            )
        if not init_path.stat().st_mode & 0o111:
            return BootstrapCheckResult(
                name="init_sh",
                status="warning",
                message="init.sh exists but is not executable — run: chmod +x init.sh",
            )
        return BootstrapCheckResult(
            name="init_sh",
            status="ok",
            message="Standard startup path available: ./init.sh",
        )

    def _check_feature_list(self) -> BootstrapCheckResult:
        feature_path = self.workspace / "feature_list.json"
        if not feature_path.exists():
            return BootstrapCheckResult(
                name="feature_list",
                status="warning",
                message="No feature_list.json found — feature state tracking not available.",
            )
        return BootstrapCheckResult(
            name="feature_list",
            status="ok",
            message="feature_list.json loaded — feature state tracking active.",
        )

    def _check_task_consistency(self, handoff: SessionHandoff | None) -> BootstrapCheckResult:
        if handoff is None or not handoff.active_tasks:
            return BootstrapCheckResult(
                name="task_consistency",
                status="ok",
                message="No active tasks to validate.",
            )

        stale_ids: list[str] = []
        if self.task_tracker is not None:
            try:
                tracker_tasks = {t.id for t in self.task_tracker.list_active()}
                for ht in handoff.active_tasks:
                    if ht.id and ht.id not in tracker_tasks:
                        stale_ids.append(ht.id)
            except Exception as e:
                logger.debug("Failed to check task consistency: {}", e)

        if stale_ids:
            return BootstrapCheckResult(
                name="task_consistency",
                status="warning",
                message=f"Handoff tasks no longer in tracker: {', '.join(stale_ids)}",
                details={"stale_ids": stale_ids},
            )

        return BootstrapCheckResult(
            name="task_consistency",
            status="ok",
            message="All handoff tasks consistent with tracker.",
        )

    def _build_context_summary(self, report: BootstrapReport) -> str:
        parts: list[str] = []

        if report.handoff_loaded and report.handoff:
            h = report.handoff
            if h.active_tasks:
                task_summaries = []
                for t in h.active_tasks:
                    task_summaries.append(f"{t.title} ({t.status})")
                parts.append(f"Resumed tasks: {'; '.join(task_summaries)}")

            if h.next_best_step:
                parts.append(f"Suggested next: {h.next_best_step}")

            if h.blockers:
                blocker_summaries = [b.description for b in h.blockers]
                parts.append(f"Known blockers: {'; '.join(blocker_summaries)}")

        if report.feature_list_loaded and report.feature_list:
            unfinished = [f for f in report.feature_list if f.status in ("not_started", "in_progress", "blocked")]
            if unfinished:
                parts.append(f"Unfinished features: {len(unfinished)}")
            if report.next_feature:
                parts.append(f"Next feature: {report.next_feature.title}")

        if report.init_sh_available:
            parts.append("Startup: ./init.sh")

        if report.warnings:
            parts.append(f"Warnings: {'; '.join(report.warnings)}")

        return " | ".join(parts) if parts else ""
