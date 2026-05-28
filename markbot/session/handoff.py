"""Session handoff — structured cross-session continuity.

Generates a handoff document at session end and loads it at session start,
ensuring the next session can pick up exactly where the last one left off
without relying solely on compressed memory summaries.

Handoff files are stored under ``workspace/sessions/handoff/`` keyed by
session key (``channel:chat_id``).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.utils.constants import USER_FILENAME
from markbot.utils.helpers import ensure_dir


@dataclass
class HandoffTask:
    id: str = ""
    title: str = ""
    status: str = "stated"
    progress: str = ""
    verification: str = ""


@dataclass
class HandoffDecision:
    summary: str = ""
    context: str = ""


@dataclass
class HandoffBlocker:
    description: str = ""
    task_id: str = ""


@dataclass
class SessionHandoff:
    session_key: str = ""
    timestamp: str = ""
    active_tasks: list[HandoffTask] = field(default_factory=list)
    key_decisions: list[HandoffDecision] = field(default_factory=list)
    blockers: list[HandoffBlocker] = field(default_factory=list)
    next_best_step: str = ""
    user_preferences_noted: list[str] = field(default_factory=list)
    cost_this_session_usd: float = 0.0
    tool_calls_this_session: int = 0

    def to_markdown(self) -> str:
        lines = [f"# Session Handoff: {self.timestamp}", ""]

        if self.active_tasks:
            lines.append("## Active Tasks")
            for t in self.active_tasks:
                lines.append(f"- [{t.status}] **{t.id}** {t.title}")
                if t.progress:
                    lines.append(f"  - Progress: {t.progress}")
                if t.verification:
                    lines.append(f"  - Verification: {t.verification}")
            lines.append("")

        if self.key_decisions:
            lines.append("## Key Decisions")
            for d in self.key_decisions:
                lines.append(f"- {d.summary}")
                if d.context:
                    lines.append(f"  Context: {d.context}")
            lines.append("")

        if self.blockers:
            lines.append("## Blockers / Unresolved")
            for b in self.blockers:
                lines.append(f"- {b.description}")
                if b.task_id:
                    lines.append(f"  (task: {b.task_id})")
            lines.append("")

        if self.next_best_step:
            lines.append("## Next Best Step")
            lines.append(self.next_best_step)
            lines.append("")

        if self.user_preferences_noted:
            lines.append("## User Preferences Noted")
            for p in self.user_preferences_noted:
                lines.append(f"- {p}")
            lines.append("")

        if self.cost_this_session_usd > 0 or self.tool_calls_this_session > 0:
            lines.append("## Session Stats")
            if self.cost_this_session_usd > 0:
                lines.append(f"- Cost: ${self.cost_this_session_usd:.4f}")
            if self.tool_calls_this_session > 0:
                lines.append(f"- Tool calls: {self.tool_calls_this_session}")
            lines.append("")

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionHandoff:
        tasks = [HandoffTask(**t) for t in data.get("active_tasks", [])]
        decisions = [HandoffDecision(**d) for d in data.get("key_decisions", [])]
        blockers = [HandoffBlocker(**b) for b in data.get("blockers", [])]
        return cls(
            session_key=data.get("session_key", ""),
            timestamp=data.get("timestamp", ""),
            active_tasks=tasks,
            key_decisions=decisions,
            blockers=blockers,
            next_best_step=data.get("next_best_step", ""),
            user_preferences_noted=data.get("user_preferences_noted", []),
            cost_this_session_usd=data.get("cost_this_session_usd", 0.0),
            tool_calls_this_session=data.get("tool_calls_this_session", 0),
        )


class HandoffManager:
    """Persist and retrieve session handoff documents."""

    _HANDOFF_DIR = "handoff"
    _MAX_HANDOFF_AGE_DAYS = 30

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._handoff_dir = workspace / "sessions" / self._HANDOFF_DIR
        ensure_dir(self._handoff_dir)

    def _session_key_to_filename(self, session_key: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_key.strip())
        return f"{safe}.json"

    def _handoff_path(self, session_key: str) -> Path:
        return self._handoff_dir / self._session_key_to_filename(session_key)

    def save(self, handoff: SessionHandoff) -> Path:
        path = self._handoff_path(handoff.session_key)
        path.write_text(
            json.dumps(handoff.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("Saved handoff for session {}", handoff.session_key)
        return path

    def load(self, session_key: str) -> SessionHandoff | None:
        path = self._handoff_path(session_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            handoff = SessionHandoff.from_dict(data)
            return handoff
        except Exception as e:
            logger.warning("Failed to load handoff for {}: {}", session_key, e)
            return None

    def load_markdown(self, session_key: str) -> str | None:
        handoff = self.load(session_key)
        if handoff is None:
            return None
        return handoff.to_markdown()

    def delete(self, session_key: str) -> None:
        path = self._handoff_path(session_key)
        if path.exists():
            path.unlink()
            logger.debug("Deleted handoff for {}", session_key)

    def cleanup_stale(self) -> int:
        now = time.time()
        cutoff = now - self._MAX_HANDOFF_AGE_DAYS * 86400
        removed = 0
        for path in self._handoff_dir.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        if removed:
            logger.info("Cleaned up {} stale handoff files", removed)
        return removed


def build_handoff_from_session(
    *,
    session_key: str,
    messages: list[dict],
    tools_used: list[str],
    cost_usd: float,
    task_tracker: Any | None = None,
    memory_manager: Any | None = None,
) -> SessionHandoff:
    """Build a SessionHandoff by extracting structured data from the
    completed session.

    Data sources:
    - Active tasks from TaskTracker (if available)
    - Key decisions from conversation messages
    - Blockers from todo items with blocked status
    - User preferences from memory manager
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    active_tasks: list[HandoffTask] = []
    blockers: list[HandoffBlocker] = []
    user_preferences: list[str] = []

    if task_tracker is not None:
        try:
            for task in task_tracker.list_active():
                active_tasks.append(HandoffTask(
                    id=task.id,
                    title=task.title,
                    status=task.status,
                    progress=task.progress,
                    verification=task.verification_result,
                ))
            for task in task_tracker.list_blocked():
                blockers.append(HandoffBlocker(
                    description=task.title or "Unknown blocker",
                    task_id=task.id,
                ))
        except Exception as e:
            logger.debug("Failed to extract tasks from tracker: {}", e)

    key_decisions = _extract_decisions(messages)

    if memory_manager is not None:
        try:
            prefs = _extract_preferences_from_memory(memory_manager)
            user_preferences.extend(prefs)
        except Exception as e:
            logger.debug("Failed to extract preferences: {}", e)

    next_step = ""
    if active_tasks:
        top = active_tasks[0]
        next_step = f"Continue task '{top.title}' (status: {top.status})"
        if top.progress:
            next_step += f" — {top.progress}"

    return SessionHandoff(
        session_key=session_key,
        timestamp=now,
        active_tasks=active_tasks,
        key_decisions=key_decisions,
        blockers=blockers,
        next_best_step=next_step,
        user_preferences_noted=user_preferences,
        cost_this_session_usd=round(cost_usd, 4),
        tool_calls_this_session=len(tools_used),
    )


def _extract_decisions(messages: list[dict]) -> list[HandoffDecision]:
    decisions: list[HandoffDecision] = []
    decision_patterns = [
        r"(?:decided|decision|let's go with|we'll use|chose|chosen)\s+(.+?)(?:\.|$)",
        r"(?:switched to|migrated to|moved to)\s+(.+?)(?:\.|$)",
    ]

    for msg in messages[-30:]:
        content = msg.get("content", "")
        if not isinstance(content, str) or not content:
            continue
        role = msg.get("role", "")
        if role not in ("assistant", "user"):
            continue
        for pattern in decision_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for m in matches:
                text = m.strip()[:200]
                if text and not any(d.summary == text for d in decisions):
                    decisions.append(HandoffDecision(
                        summary=text,
                        context="user" if role == "user" else "assistant",
                    ))
        if len(decisions) >= 5:
            break

    return decisions


def _extract_preferences_from_memory(memory_manager: Any) -> list[str]:
    prefs: list[str] = []
    try:
        profile_path = Path(memory_manager.working_dir) / USER_FILENAME
        if not profile_path.exists():
            fallback_path = Path(memory_manager.working_dir) / "USER.md"
            if fallback_path.exists():
                profile_path = fallback_path
        if profile_path.exists():
            content = profile_path.read_text(encoding="utf-8").strip()
            if content:
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("- ") or line.startswith("* "):
                        prefs.append(line.lstrip("-* ").strip())
    except Exception:
        pass
    return prefs[:10]
