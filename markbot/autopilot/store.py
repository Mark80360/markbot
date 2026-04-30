"""Autopilot Store — persistent state management for the autopilot pipeline."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import uuid4

from markbot.autopilot.types import (
    AutopilotConfig,
    AutopilotPolicy,
    TaskCard,
    TaskJournalEntry,
    TaskRegistry,
    TaskSource,
    TaskStatus,
    VerificationPolicy,
)
from markbot.utils.helpers import ensure_dir

_SOURCE_BASE_SCORES: dict[TaskSource, int] = {
    "manual_idea": 80,
    "github_issue": 75,
    "github_pr": 85,
    "agent_candidate": 45,
    "cron_trigger": 60,
}

_BUG_HINTS = ("bug", "fix", "failure", "broken", "regression", "crash", "error", "issue")
_URGENT_HINTS = ("urgent", "p0", "p1", "high", "critical", "blocker")


def _shorten(text: str, *, limit: int = 120) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _safe_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _card_to_dict(card: TaskCard) -> dict[str, Any]:
    return asdict(card)


def _dict_to_card(data: dict[str, Any]) -> TaskCard:
    return TaskCard(**data)


def _entry_to_dict(entry: TaskJournalEntry) -> dict[str, Any]:
    return asdict(entry)


def _dict_to_entry(data: dict[str, Any]) -> TaskJournalEntry:
    return TaskJournalEntry(**data)


class AutopilotStore:
    """Persist and query autopilot state within a MarkBot workspace."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).resolve()
        self._autopilot_dir = self._workspace / "autopilot"
        self._registry_path = self._autopilot_dir / "registry.json"
        self._journal_path = self._autopilot_dir / "journal.jsonl"
        self._context_path = self._autopilot_dir / "active_context.md"
        self._runs_dir = self._autopilot_dir / "runs"
        self._policy_path = self._autopilot_dir / "policy.json"
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        ensure_dir(self._autopilot_dir)
        ensure_dir(self._runs_dir)
        if not self._registry_path.exists():
            self._save_registry(TaskRegistry())

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def autopilot_dir(self) -> Path:
        return self._autopilot_dir

    @property
    def runs_dir(self) -> Path:
        return self._runs_dir

    def _load_registry(self) -> TaskRegistry:
        if not self._registry_path.exists():
            return TaskRegistry()
        try:
            data = json.loads(self._registry_path.read_text(encoding="utf-8"))
            cards = [_dict_to_card(c) for c in data.get("cards", [])]
            return TaskRegistry(
                version=data.get("version", 1),
                updated_at=data.get("updated_at", 0.0),
                cards=cards,
            )
        except Exception:
            return TaskRegistry()

    def _save_registry(self, registry: TaskRegistry) -> None:
        registry.updated_at = time.time()
        data = {
            "version": registry.version,
            "updated_at": registry.updated_at,
            "cards": [_card_to_dict(c) for c in registry.cards],
        }
        self._registry_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def list_cards(self, *, status: TaskStatus | None = None) -> list[TaskCard]:
        cards = self._load_registry().cards
        if status is not None:
            cards = [c for c in cards if c.status == status]
        return sorted(cards, key=lambda c: (-c.score, -c.updated_at, c.title.lower()))

    def get_card(self, card_id: str) -> TaskCard | None:
        for card in self._load_registry().cards:
            if card.id == card_id:
                return card
        return None

    def _build_fingerprint(
        self,
        *,
        source_kind: TaskSource,
        source_ref: str,
        title: str,
        body: str,
    ) -> str:
        payload = f"{source_kind}:{source_ref}:{title}:{body}"
        return sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _normalize_labels(self, labels: list[str] | None) -> list[str]:
        if not labels:
            return []
        return [label.strip() for label in labels if label.strip()]

    def _merge_labels(self, existing: list[str], incoming: list[str]) -> list[str]:
        merged = list(existing)
        for label in incoming:
            if label not in merged:
                merged.append(label)
        return merged

    def _score_card(self, card: TaskCard) -> tuple[int, list[str]]:
        score = _SOURCE_BASE_SCORES.get(card.source_kind, 50)
        base_score = _SOURCE_BASE_SCORES.get(card.source_kind, 50)
        reasons: list[str] = [f"source:{card.source_kind}={base_score}"]

        title_lower = card.title.lower()
        body_lower = card.body.lower()
        combined = f"{title_lower} {body_lower}"

        for hint in _BUG_HINTS:
            if hint in combined:
                score += 15
                reasons.append(f"bug_hint:{hint}=+15")
                break

        for hint in _URGENT_HINTS:
            if hint in combined:
                score += 20
                reasons.append(f"urgent_hint:{hint}=+20")
                break

        for label in card.labels:
            label_lower = label.lower()
            if label_lower in _URGENT_HINTS:
                score += 20
                reasons.append(f"urgent_label:{label}=+20")
            elif label_lower in _BUG_HINTS:
                score += 15
                reasons.append(f"bug_label:{label}=+15")

        if card.status in ("failed", "rejected"):
            score = max(score - 30, 0)
            reasons.append("previously_failed=-30")

        return score, reasons

    def enqueue_card(
        self,
        *,
        source_kind: TaskSource,
        title: str,
        body: str = "",
        source_ref: str = "",
        labels: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[TaskCard, bool]:
        registry = self._load_registry()
        now = time.time()
        normalized_title = title.strip()
        normalized_body = body.strip()
        normalized_ref = source_ref.strip()
        fingerprint = self._build_fingerprint(
            source_kind=source_kind,
            source_ref=normalized_ref,
            title=normalized_title,
            body=normalized_body,
        )

        existing = next((c for c in registry.cards if c.fingerprint == fingerprint), None)
        merged_labels = self._normalize_labels(labels)
        merged_metadata = dict(metadata or {})

        if existing is not None:
            if normalized_title:
                existing.title = normalized_title
            if normalized_body:
                existing.body = normalized_body
            if normalized_ref:
                existing.source_ref = normalized_ref
            existing.labels = self._merge_labels(existing.labels, merged_labels)
            existing.metadata.update(merged_metadata)
            existing.updated_at = now
            existing.score, existing.score_reasons = self._score_card(existing)
            self._save_registry(registry)
            self.append_journal(
                kind="intake_refresh",
                summary=f"Refreshed intake card {existing.id}: {existing.title}",
                task_id=existing.id,
            )
            self.rebuild_active_context()
            return existing, False

        card = TaskCard(
            id=f"ap-{uuid4().hex[:8]}",
            fingerprint=fingerprint,
            title=normalized_title or "Untitled task",
            body=normalized_body,
            source_kind=source_kind,
            source_ref=normalized_ref,
            labels=merged_labels,
            metadata=merged_metadata,
            created_at=now,
            updated_at=now,
        )
        card.score, card.score_reasons = self._score_card(card)
        registry.cards.append(card)
        self._save_registry(registry)
        self.append_journal(
            kind="intake_added",
            summary=f"Queued {card.source_kind}: {card.title}",
            task_id=card.id,
            metadata={"source_ref": card.source_ref, "score": card.score},
        )
        self.rebuild_active_context()
        return card, True

    def pick_next_card(self) -> TaskCard | None:
        eligible = [c for c in self._load_registry().cards if c.status in ("queued", "accepted")]
        if not eligible:
            return None
        return sorted(eligible, key=lambda c: (-c.score, -c.updated_at, c.title.lower()))[0]

    def update_status(
        self,
        card_id: str,
        *,
        status: TaskStatus,
        note: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> TaskCard:
        registry = self._load_registry()
        card = next((c for c in registry.cards if c.id == card_id), None)
        if card is None:
            raise ValueError(f"No autopilot card found with ID: {card_id}")
        card.status = status
        card.updated_at = time.time()
        if note:
            card.metadata["last_note"] = note.strip()
        if metadata_updates:
            card.metadata.update(metadata_updates)
        card.score, card.score_reasons = self._score_card(card)
        self._save_registry(registry)
        summary = f"{status}: {card.title}"
        if note:
            summary = f"{summary} ({_shorten(note, limit=80)})"
        self.append_journal(kind=f"status_{status}", summary=summary, task_id=card.id)
        self.rebuild_active_context()
        return card

    def load_journal(self, *, limit: int = 12) -> list[TaskJournalEntry]:
        if not self._journal_path.exists():
            return []
        entries: list[TaskJournalEntry] = []
        for line in self._journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(_dict_to_entry(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return entries[-limit:]

    def append_journal(
        self,
        *,
        kind: str,
        summary: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskJournalEntry:
        entry = TaskJournalEntry(
            timestamp=time.time(),
            kind=kind,
            summary=summary.strip(),
            task_id=task_id,
            metadata=metadata or {},
        )
        with self._journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_entry_to_dict(entry), ensure_ascii=False) + "\n")
        return entry

    def load_active_context(self) -> str:
        if not self._context_path.exists():
            return ""
        return self._context_path.read_text(encoding="utf-8", errors="replace").strip()

    def rebuild_active_context(self) -> str:
        cards = self._load_registry().cards
        _active = ("preparing", "running", "verifying", "repairing")
        running = [c for c in cards if c.status in _active]
        accepted = [c for c in cards if c.status == "accepted"]
        queued = [c for c in cards if c.status == "queued"]
        completed = [c for c in cards if c.status == "completed"]
        failed = [c for c in cards if c.status in ("failed", "rejected")]

        focus = None
        for group in (running, accepted, queued):
            if group:
                focus = sorted(group, key=lambda c: (-c.score, -c.updated_at, c.title.lower()))[0]
                break

        lines = [
            "# Active Autopilot Context",
            "",
            f"Generated at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
            "",
            "## Current Focus",
        ]
        if focus is None:
            lines.append("- No active task focus yet.")
        else:
            lines.append(
                f"- [{focus.status}] {focus.title} "
                f"({focus.source_kind}, score={focus.score})"
            )
            if focus.body:
                lines.append(f"- Detail: {_shorten(focus.body, limit=220)}")

        lines.extend(["", "## In Progress"])
        for c in sorted(running + accepted, key=lambda item: (-item.score, -item.updated_at))[:6]:
            lines.append(f"- [{c.status}] {c.id} {c.title} ({c.source_kind})")
        if not running and not accepted:
            lines.append("- None.")

        lines.extend(["", "## Next Up"])
        for c in sorted(queued, key=lambda item: (-item.score, -item.updated_at))[:8]:
            lines.append(f"- [{c.score}] {c.id} {c.title} ({c.source_kind})")
        if not queued:
            lines.append("- No queued items.")

        lines.extend(["", "## Recently Completed"])
        for c in sorted(completed, key=lambda item: item.updated_at, reverse=True)[:5]:
            lines.append(f"- {c.id} {c.title}")
        if not completed:
            lines.append("- None yet.")

        lines.extend(["", "## Recent Failures"])
        for c in sorted(failed, key=lambda item: item.updated_at, reverse=True)[:5]:
            lines.append(f"- [{c.status}] {c.id} {c.title}")
        if not failed:
            lines.append("- None.")

        lines.extend(["", "## Recent Journal"])
        journal = self.load_journal(limit=8)
        if journal:
            for entry in journal:
                lines.append(
                    f"- {time.strftime('%m-%d %H:%M', time.gmtime(entry.timestamp))} "
                    f"{entry.kind}: {entry.summary}"
                )
        else:
            lines.append("- Journal is empty.")

        content = "\n".join(lines).strip() + "\n"
        self._context_path.write_text(content, encoding="utf-8")
        return content

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for card in self._load_registry().cards:
            counts[card.status] = counts.get(card.status, 0) + 1
        return counts

    def load_config(self) -> AutopilotConfig:
        if not self._policy_path.exists():
            return AutopilotConfig()
        try:
            data = json.loads(self._policy_path.read_text(encoding="utf-8"))
            ap_data = data.get("autopilot_policy", {})
            vp_data = data.get("verification_policy", {})
            return AutopilotConfig(
                autopilot_policy=AutopilotPolicy(**ap_data),
                verification_policy=VerificationPolicy(**vp_data),
            )
        except Exception:
            return AutopilotConfig()

    def save_config(self, config: AutopilotConfig) -> None:
        data = {
            "autopilot_policy": asdict(config.autopilot_policy),
            "verification_policy": asdict(config.verification_policy),
        }
        self._policy_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
