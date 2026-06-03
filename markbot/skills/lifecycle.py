"""Skill lifecycle management — state transitions based on usage.

Implements a state machine: active → stale → archived.
State is determined by usage metrics (view_count, use_count, last_activity_at)
and can be transitioned manually or by the Curator.

Archived skills are moved to a subdirectory rather than deleted.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from markbot.skills.usage import SkillUsageStore
from markbot.types.skill import SkillState

# Thresholds (seconds)
STALE_THRESHOLD = 30 * 24 * 3600   # 30 days of inactivity
ARCHIVE_THRESHOLD = 90 * 24 * 3600  # 90 days of inactivity
NEW_SKILL_GRACE_PERIOD = 14 * 24 * 3600  # 14 days before zero-use skills go stale


@dataclass
class TransitionReport:
    """Report of a proposed or executed state transition."""

    skill_name: str
    current_state: str
    target_state: str
    reason: str
    applied: bool = False


class SkillLifecycle:
    """Manages skill lifecycle states based on usage metrics.

    States:
      - active:   recently used or newly created
      - stale:    not used for a long time, or never used after grace period
      - archived: inactive for a very long time, or manually archived
    """

    def __init__(self, workspace: Path, usage_store: SkillUsageStore):
        self._workspace = workspace
        self._skills_dir = workspace / "skills"
        self._archived_dir = workspace / "skills" / "archived"
        self._usage_store = usage_store

    def evaluate(self, skill_name: str, is_builtin: bool = False) -> str:
        """Evaluate the lifecycle state of a skill.

        Args:
            skill_name: Name of the skill.
            is_builtin: If True, always returns ACTIVE (builtins don't age).

        Returns:
            One of SkillState.ACTIVE, STALE, or ARCHIVED.
        """
        if is_builtin:
            return SkillState.ACTIVE

        entry = self._usage_store.get(skill_name)
        now = time.time()

        # If archived directory exists for this skill, it's archived
        if (self._archived_dir / skill_name).exists():
            return SkillState.ARCHIVED

        # Never used: check grace period
        if entry.use_count == 0 and entry.view_count == 0:
            age = now - entry.created_at
            if age > NEW_SKILL_GRACE_PERIOD:
                return SkillState.STALE
            return SkillState.ACTIVE

        # Check inactivity
        if entry.last_activity_at is not None:
            inactive_duration = now - entry.last_activity_at
            if inactive_duration > ARCHIVE_THRESHOLD:
                return SkillState.ARCHIVED
            if inactive_duration > STALE_THRESHOLD:
                return SkillState.STALE

        return SkillState.ACTIVE

    def scan_all(self, skills: list[tuple[str, bool]]) -> list[TransitionReport]:
        """Scan all skills and report proposed state transitions.

        Args:
            skills: List of (skill_name, is_builtin) tuples.

        Returns:
            List of TransitionReport for skills that need state changes.
        """
        reports = []
        for skill_name, is_builtin in skills:
            current = self.evaluate(skill_name, is_builtin)
            # Get the skill's stored state from usage store
            entry = self._usage_store.get(skill_name)
            stored_state = getattr(entry, 'state', SkillState.ACTIVE)

            if current != stored_state:
                reports.append(TransitionReport(
                    skill_name=skill_name,
                    current_state=stored_state,
                    target_state=current,
                    reason=self._explain_transition(stored_state, current, entry),
                ))
        return reports

    def transition(self, skill_name: str, target_state: str) -> TransitionReport:
        """Execute a state transition for a skill.

        For archived state, moves the skill directory to skills/archived/.
        For active/stale, updates the stored state only.

        Args:
            skill_name: Name of the skill to transition.
            target_state: Target SkillState.

        Returns:
            TransitionReport with applied=True if successful.
        """
        current_state = self.evaluate(skill_name)
        if current_state == target_state:
            return TransitionReport(
                skill_name=skill_name,
                current_state=current_state,
                target_state=target_state,
                reason="Already in target state",
                applied=True,
            )

        if target_state == SkillState.ARCHIVED:
            return self._archive_skill(skill_name, current_state)
        elif target_state == SkillState.ACTIVE:
            return self._activate_skill(skill_name, current_state)
        else:
            # Stale is just a metadata update
            self._usage_store.set_state(skill_name, SkillState.STALE)
            return TransitionReport(
                skill_name=skill_name,
                current_state=current_state,
                target_state=SkillState.STALE,
                reason="Marked as stale",
                applied=True,
            )

    def _archive_skill(self, skill_name: str, current_state: str) -> TransitionReport:
        """Move a skill to the archived directory."""
        source = self._skills_dir / skill_name
        if not source.exists():
            return TransitionReport(
                skill_name=skill_name,
                current_state=current_state,
                target_state=SkillState.ARCHIVED,
                reason=f"Skill directory not found: {source}",
                applied=False,
            )

        self._archived_dir.mkdir(parents=True, exist_ok=True)
        dest = self._archived_dir / skill_name
        if dest.exists():
            return TransitionReport(
                skill_name=skill_name,
                current_state=current_state,
                target_state=SkillState.ARCHIVED,
                reason=f"Archived directory already exists: {dest}",
                applied=False,
            )

        shutil.move(str(source), str(dest))
        logger.info("Archived skill '{}' to {}", skill_name, dest)

        self._usage_store.set_state(skill_name, SkillState.ARCHIVED)

        return TransitionReport(
            skill_name=skill_name,
            current_state=current_state,
            target_state=SkillState.ARCHIVED,
            reason="Moved to archived directory",
            applied=True,
        )

    def _activate_skill(self, skill_name: str, current_state: str) -> TransitionReport:
        """Restore a skill from archived to active."""
        source = self._archived_dir / skill_name
        dest = self._skills_dir / skill_name

        if source.exists():
            if dest.exists():
                return TransitionReport(
                    skill_name=skill_name,
                    current_state=current_state,
                    target_state=SkillState.ACTIVE,
                    reason=f"Active directory already exists: {dest}",
                    applied=False,
                )
            shutil.move(str(source), str(dest))
            logger.info("Restored skill '{}' from archive", skill_name)
        elif not dest.exists():
            return TransitionReport(
                skill_name=skill_name,
                current_state=current_state,
                target_state=SkillState.ACTIVE,
                reason="Skill not found in archived or active directories",
                applied=False,
            )

        self._usage_store.set_state(skill_name, SkillState.ACTIVE)

        return TransitionReport(
            skill_name=skill_name,
            current_state=current_state,
            target_state=SkillState.ACTIVE,
            reason="Restored to active",
            applied=True,
        )

    @staticmethod
    def _explain_transition(from_state: str, to_state: str, entry) -> str:
        """Generate a human-readable explanation for a state transition."""
        now = time.time()
        if to_state == SkillState.STALE:
            if entry.use_count == 0:
                age_days = int((now - entry.created_at) / 86400)
                return f"Never used after {age_days} days"
            inactive_days = int((now - (entry.last_activity_at or 0)) / 86400)
            return f"Inactive for {inactive_days} days"
        elif to_state == SkillState.ARCHIVED:
            inactive_days = int((now - (entry.last_activity_at or entry.created_at)) / 86400)
            return f"Inactive for {inactive_days} days (>{ARCHIVE_THRESHOLD // 86400} day threshold)"
        elif to_state == SkillState.ACTIVE:
            return "Usage detected or manual activation"
        return f"Transition from {from_state} to {to_state}"


__all__ = ["SkillLifecycle", "TransitionReport"]
