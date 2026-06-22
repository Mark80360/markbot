"""Curator — background skill maintenance service.

Periodically scans skills for health, auto-archives stale skills,
and generates maintenance reports. Runs as a background task
(similar to DreamService).

Responsibilities:
  - Scan all skills and evaluate lifecycle states
  - Auto-archive skills that exceed the archive threshold
  - Generate maintenance reports
  - Trigger skill improvement (via SkillImprover) for stale skills
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class CuratorReport:
    """Report from a single curator maintenance run."""

    timestamp: float = field(default_factory=time.time)
    skills_scanned: int = 0
    transitions: list[dict[str, Any]] = field(default_factory=list)
    improvements: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CuratorService:
    """Background service for skill health maintenance.

    Usage:
        curator = CuratorService(workspace, skill_registry, interval_hours=6)
        await curator.start()
        ...
        await curator.stop()
        # Or run once:
        report = curator.run_maintenance()
    """

    def __init__(
        self,
        workspace: Path,
        skill_registry: Any = None,
        auto_archive: bool = True,
        interval_hours: int = 6,
    ):
        self._workspace = workspace
        self._registry = skill_registry
        self._auto_archive = auto_archive
        self._interval_hours = interval_hours
        self._reports_dir = workspace / ".markbot" / "curator_reports"
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def run_maintenance(self) -> CuratorReport:
        """Execute a full maintenance cycle.

        Returns:
            CuratorReport with details of actions taken.
        """
        report = CuratorReport()

        if not self._registry:
            report.errors.append("No skill registry available")
            return report

        try:
            from markbot.skills.lifecycle import SkillLifecycle

            usage_store = self._registry.usage_store
            lifecycle = SkillLifecycle(self._workspace, usage_store)

            # Collect all skills
            skills = self._registry.list_all()
            report.skills_scanned = len(skills)

            # Scan for needed transitions
            skill_tuples = [(s.name, s.is_builtin) for s in skills]
            transition_reports = lifecycle.scan_all(skill_tuples)

            for tr in transition_reports:
                report.transitions.append({
                    "skill": tr.skill_name,
                    "from": tr.current_state,
                    "to": tr.target_state,
                    "reason": tr.reason,
                })

                # Auto-apply if auto_archive is enabled
                if self._auto_archive and tr.target_state in ("stale", "archived"):
                    # Look up is_builtin from the skill list so we don't
                    # accidentally archive a builtin skill.
                    skill_def = next((s for s in skills if s.name == tr.skill_name), None)
                    is_builtin = skill_def.is_builtin if skill_def else False
                    result = lifecycle.transition(tr.skill_name, tr.target_state, is_builtin=is_builtin)
                    if result.applied:
                        report.transitions[-1]["applied"] = True
                        logger.info(
                            "Curator: {} -> {} ({})",
                            tr.skill_name, tr.target_state, tr.reason,
                        )
                        # After archiving, unload the skill from the registry
                        # so its tools are removed and the agent can't call
                        # scripts whose files have been moved.
                        if tr.target_state == "archived" and self._registry is not None:
                            self._registry.unload_skill(tr.skill_name)
                            logger.info(
                                "Curator: unloaded skill '{}' from registry",
                                tr.skill_name,
                            )
                    else:
                        report.transitions[-1]["applied"] = False
                        report.transitions[-1]["error"] = result.reason

            # Evaluate stale skills for improvement
            self._evaluate_improvements(skills, report)

        except Exception as e:
            logger.exception("Curator maintenance failed")
            report.errors.append(str(e))

        # Persist report
        self._save_report(report)

        return report

    def _evaluate_improvements(
        self,
        skills: list[Any],
        report: CuratorReport,
    ) -> None:
        """Evaluate stale skills and generate improvement suggestions."""
        try:
            from markbot.skills.improve import SkillImprover

            improver = SkillImprover(self._workspace)

            for skill in skills:
                # Only evaluate non-builtin, non-archived skills that are
                # stale or low-quality. Archived skills have already been
                # moved out of the active set and shouldn't be scored.
                if skill.is_builtin:
                    continue
                if getattr(skill, "state", None) == "archived":
                    continue

                eval_result = improver.run_eval(skill.name, skill)

                if eval_result.score < 0.6 or eval_result.issues:
                    report.improvements.append({
                        "skill": skill.name,
                        "score": eval_result.score,
                        "issues": eval_result.issues,
                        "suggestions": eval_result.suggestions,
                    })
                    logger.info(
                        "Curator: skill '{}' scored {:.1f}, {} issues found",
                        skill.name, eval_result.score, len(eval_result.issues),
                    )

        except Exception as e:
            logger.debug("Skill improvement evaluation failed: {}", e)

    async def start(self) -> None:
        """Start the curator background loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Curator background service started (interval={}h)", self._interval_hours,
        )

    async def stop(self) -> None:
        """Stop the curator background loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
            logger.info("Curator background service stopped")

    async def _run(self) -> None:
        """Run maintenance on an interval."""
        interval_s = self._interval_hours * 3600
        while self._running:
            try:
                report = self.run_maintenance()
                if report.transitions:
                    logger.info(
                        "Curator: {} transitions applied",
                        sum(1 for t in report.transitions if t.get("applied")),
                    )
                if report.errors:
                    logger.warning("Curator: {} errors during maintenance", len(report.errors))
            except Exception as e:
                logger.error("Curator background maintenance failed: {}", e)
            await asyncio.sleep(interval_s)

    def get_recent_reports(self, limit: int = 5) -> list[dict[str, Any]]:
        """Load recent curator reports from disk."""
        if not self._reports_dir.exists():
            return []

        reports = []
        for f in sorted(self._reports_dir.glob("*.json"), reverse=True)[:limit]:
            try:
                content = f.read_text(encoding="utf-8")
                reports.append(json.loads(content))
            except Exception:
                continue
        return reports

    def _save_report(self, report: CuratorReport) -> None:
        """Persist a curator report to disk."""
        try:
            self._reports_dir.mkdir(parents=True, exist_ok=True)
            filename = f"curator_{int(report.timestamp)}.json"
            content = json.dumps(asdict(report), indent=2, ensure_ascii=False)

            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp",
                prefix="curator_",
                dir=str(self._reports_dir),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_path, self._reports_dir / filename)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning("Failed to save curator report: {}", e)


__all__ = ["CuratorService", "CuratorReport"]
