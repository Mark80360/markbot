"""Todo tool for structured task tracking.

Provides a persistent, queryable task list that the agent can use
to track progress across multi-step workflows.  Tasks are stored
as JSON so they can be filtered by status / priority / session.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.agent.tools.base import Tool
from markbot.config.paths import get_workspace_path


_TODO_DIR_NAME = "todos"
_INDEX_FILE = "index.json"

_STATUS_ORDER = {"in_progress": 0, "pending": 1, "completed": 2}
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _now_iso() -> str:
    return datetime.now().isoformat()


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


class _TodoStore:
    """Simple JSON-file backed store for todo items."""

    def __init__(self, workspace: Path):
        self._dir = workspace / _TODO_DIR_NAME
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / _INDEX_FILE
        self._items: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if self._index_path.exists():
            try:
                data = json.loads(self._index_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except Exception as exc:
                logger.warning("Failed to load todo index: {}", exc)
        return []

    def _save(self) -> None:
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._index_path)

    def write(self, items: list[dict[str, Any]], session_id: str | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in items:
            item_id = item.get("id")
            if item_id:
                updated = self._update(item_id, item)
                if updated:
                    results.append(updated)
                else:
                    results.append({"error": f"Todo item '{item_id}' not found"})
            else:
                created = self._create(item, session_id)
                results.append(created)
        self._save()
        return results

    def _create(self, item: dict[str, Any], session_id: str | None) -> dict[str, Any]:
        now = _now_iso()
        new_item: dict[str, Any] = {
            "id": _short_id(),
            "content": item.get("content", ""),
            "status": item.get("status", "pending"),
            "priority": item.get("priority", "medium"),
            "session_id": session_id,
            "created_at": now,
            "updated_at": now,
        }
        self._items.append(new_item)
        return new_item

    def _update(self, item_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        for existing in self._items:
            if existing.get("id") == item_id:
                for key in ("content", "status", "priority"):
                    if key in patch:
                        existing[key] = patch[key]
                existing["updated_at"] = _now_iso()
                return existing
        return None

    def list_items(self, status: str | None = None, priority: str | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        filtered = self._items
        if status:
            filtered = [i for i in filtered if i.get("status") == status]
        if priority:
            filtered = [i for i in filtered if i.get("priority") == priority]
        if session_id:
            filtered = [i for i in filtered if i.get("session_id") == session_id]
        filtered = sorted(
            filtered,
            key=lambda i: (
                _STATUS_ORDER.get(i.get("status", "pending"), 99),
                _PRIORITY_ORDER.get(i.get("priority", "medium"), 99),
            ),
        )
        return filtered

    def delete(self, ids: list[str]) -> list[dict[str, Any]]:
        id_set = set(ids)
        removed: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for item in self._items:
            (removed if item.get("id") in id_set else remaining).append(item)
        self._items = remaining
        self._save()
        return removed


class TodoTool(Tool):
    """Structured task tracking tool.

    Actions:
      - write:  create new todo items or update existing ones (by id)
      - list:   query items with optional filters
      - delete: remove items by id
    """

    _is_lightweight_tool = True

    def __init__(self, workspace: Path | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._store: _TodoStore | None = None
        self._workspace = workspace
        self._session_id: str | None = None

    def _get_store(self) -> _TodoStore:
        if self._store is None:
            ws = self._workspace or get_workspace_path()
            self._store = _TodoStore(ws)
        return self._store

    def set_session(self, session_id: str | None) -> None:
        self._session_id = session_id

    @property
    def name(self) -> str:
        return "todo"

    @property
    def description(self) -> str:
        return (
            "Structured task tracking tool for managing todo items. "
            "WHEN to use:\n"
            "- User explicitly asks to record, track, or list tasks/todos\n"
            "- A task requires 3+ steps with dependencies between them\n"
            "- You need to persist progress across multiple tool-call turns\n"
            "WHEN NOT to use:\n"
            "- Single-step operations or simple Q&A\n"
            "- One-off commands with no follow-up\n"
            "- Scheduled/future reminders (use `cron` instead)\n"
            "Actions:\n"
            "- write: create new items or update existing ones (provide id to update)\n"
            "- list: query items with optional filters (status, priority)\n"
            "- delete: remove items by id\n"
            "Do NOT create markdown files as task trackers — use this tool instead."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["write", "list", "delete"],
                    "description": "Action to perform",
                },
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Existing item id (for updates). Omit to create new.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Task description",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "Item status (default: pending)",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "Item priority (default: medium)",
                            },
                        },
                        "required": ["content"],
                    },
                    "description": "Items to create or update (for write action)",
                },
                "filter": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "Filter by status",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "Filter by priority",
                        },
                    },
                    "description": "Filters for list action",
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Item ids to delete (for delete action)",
                },
            },
            "required": ["action"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action", "")
        store = self._get_store()

        if action == "write":
            items = kwargs.get("items", [])
            if not items:
                return "Error: items is required for write action"
            results = store.write(items, session_id=self._session_id)
            return self._format_write_results(results)

        if action == "list":
            filt = kwargs.get("filter", {}) or {}
            items = store.list_items(
                status=filt.get("status"),
                priority=filt.get("priority"),
                session_id=filt.get("session_id") if "session_id" in filt else None,
            )
            return self._format_list_results(items)

        if action == "delete":
            ids = kwargs.get("ids", [])
            if not ids:
                return "Error: ids is required for delete action"
            removed = store.delete(ids)
            return self._format_delete_results(removed)

        return f"Error: unknown action '{action}'"

    @staticmethod
    def _format_write_results(results: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for r in results:
            if "error" in r:
                lines.append(f"Error: {r['error']}")
            else:
                status = r.get("status", "pending")
                priority = r.get("priority", "medium")
                lines.append(f"{'✓' if status == 'completed' else '○' if status == 'pending' else '→'} [{r['id']}] {r['content']} (status={status}, priority={priority})")
        return "\n".join(lines)

    @staticmethod
    def _format_list_results(items: list[dict[str, Any]]) -> str:
        if not items:
            return "No todo items found."
        lines: list[str] = []
        for item in items:
            status = item.get("status", "pending")
            priority = item.get("priority", "medium")
            icon = "✓" if status == "completed" else "→" if status == "in_progress" else "○"
            pid = item.get("id", "?")
            content = item.get("content", "")
            lines.append(f"{icon} [{pid}] {content} (status={status}, priority={priority})")
        return "\n".join(lines)

    @staticmethod
    def _format_delete_results(removed: list[dict[str, Any]]) -> str:
        if not removed:
            return "No items deleted (ids not found)."
        lines = [f"Deleted [{r.get('id', '?')}] {r.get('content', '')}" for r in removed]
        return "\n".join(lines)
