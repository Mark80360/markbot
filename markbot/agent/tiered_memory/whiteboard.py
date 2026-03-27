"""L1 Whiteboard Memory - Loop-level temporary workspace."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .base import BaseMemoryLayer, MemoryTier


class WhiteboardMemory(BaseMemoryLayer):
    """
    L1 Whiteboard: Loop-level temporary workspace.
    
    - Only lives during a single Agent Loop execution
    - Cleared automatically after Loop ends
    - Supports checkpoint for resumable loops
    - Stores: task state, execution plan, intermediate results
    
    Storage: In-memory with optional checkpoint persistence
    """
    
    def __init__(self, workspace_path: str, chat_id: str):
        super().__init__(MemoryTier.WHITEBOARD, workspace_path)
        self.chat_id = chat_id
        self._data: Dict[str, Any] = {}
        self._init_default_structure()
        
        # Checkpoint path for resumable loops
        self._checkpoint_path = Path(workspace_path) / "memory" / "checkpoints" / f"{chat_id}.json"
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _init_default_structure(self) -> None:
        """Initialize default whiteboard structure."""
        self._data = {
            "task_specification": "",
            "execution_plan": [],
            "current_state": "INITIAL",
            "loop_counter": 0,
            "completed_subtasks": [],
            "pending_subtasks": [],
            "intermediate_results": {},
            "content_registry": {},  # Track what content has been processed
            "checkpoint_data": {},
            "notes": [],  # Agent notes during execution
        }
    
    def update(self, key: str, value: Any) -> None:
        """Update a whiteboard field."""
        self._data[key] = value
        # Auto-increment loop counter on state changes
        if key == "current_state":
            self._data["loop_counter"] = self._data.get("loop_counter", 0) + 1
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a whiteboard field."""
        return self._data.get(key, default)
    
    def add_note(self, note: str) -> None:
        """Add a note to the whiteboard."""
        timestamp = time.strftime("%H:%M:%S")
        self._data["notes"].append(f"[{timestamp}] {note}")
    
    def add_completed_subtask(self, subtask: str, result: str) -> None:
        """Mark a subtask as completed."""
        self._data["completed_subtasks"].append({
            "task": subtask,
            "result": result,
            "timestamp": time.time()
        })
        # Remove from pending if present
        if subtask in self._data["pending_subtasks"]:
            self._data["pending_subtasks"].remove(subtask)
    
    def add_pending_subtask(self, subtask: str) -> None:
        """Add a subtask to pending list."""
        if subtask not in self._data["pending_subtasks"]:
            self._data["pending_subtasks"].append(subtask)
    
    def store_result(self, key: str, result: Any) -> None:
        """Store intermediate result."""
        self._data["intermediate_results"][key] = result
    
    def get_result(self, key: str) -> Any:
        """Get intermediate result."""
        return self._data["intermediate_results"].get(key)
    
    def ensure_task_frame(self) -> None:
        """Ensure task frame structure exists (called at loop start)."""
        required_keys = [
            "task_specification",
            "execution_plan", 
            "current_state",
            "loop_counter",
            "completed_subtasks",
            "pending_subtasks",
            "intermediate_results",
            "content_registry",
        ]
        for key in required_keys:
            if key not in self._data:
                self._data[key] = [] if key in ["execution_plan", "completed_subtasks", "pending_subtasks"] else {}
                if key == "current_state":
                    self._data[key] = "INITIAL"
                if key == "loop_counter":
                    self._data[key] = 0
    
    def save_checkpoint(self) -> None:
        """Save current state to checkpoint file."""
        checkpoint = {
            "chat_id": self.chat_id,
            "timestamp": time.time(),
            "data": self._data
        }
        self._checkpoint_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    
    def load_checkpoint(self) -> bool:
        """Load state from checkpoint file. Returns True if loaded."""
        if not self._checkpoint_path.exists():
            return False
        try:
            checkpoint = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
            if checkpoint.get("chat_id") == self.chat_id:
                self._data = checkpoint.get("data", {})
                self.ensure_task_frame()
                return True
        except Exception:
            pass
        return False
    
    def clear_checkpoint(self) -> None:
        """Remove checkpoint file."""
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()
    
    # BaseMemoryLayer interface implementation
    
    def add(self, content: str, **metadata) -> None:
        """Add content as a note."""
        self.add_note(content)
    
    def get_context(self, query: Optional[str] = None, limit: int = 10) -> str:
        """Get whiteboard context as formatted string."""
        lines = ["## Whiteboard Context"]
        lines.append(f"**State**: {self._data.get('current_state', 'UNKNOWN')}")
        lines.append(f"**Loop**: {self._data.get('loop_counter', 0)}")
        
        if self._data.get("task_specification"):
            lines.append(f"\n**Task**: {self._data['task_specification']}")
        
        if self._data.get("pending_subtasks"):
            lines.append("\n**Pending Subtasks**:")
            for task in self._data["pending_subtasks"][-limit:]:
                lines.append(f"- {task}")
        
        if self._data.get("completed_subtasks"):
            lines.append("\n**Completed**:")
            for task in self._data["completed_subtasks"][-limit:]:
                lines.append(f"- {task.get('task', 'unknown')}")
        
        if self._data.get("intermediate_results"):
            results = self._data["intermediate_results"]
            if results:
                lines.append("\n**Intermediate Results**:")
                for key, value in list(results.items())[:limit]:
                    value_str = str(value)[:100] + "..." if len(str(value)) > 100 else str(value)
                    lines.append(f"- {key}: {value_str}")
        
        return "\n".join(lines)
    
    def clear(self) -> None:
        """Clear all whiteboard data."""
        self._data.clear()
        self._init_default_structure()
    
    @property
    def is_persistent(self) -> bool:
        """Whiteboard only persists via checkpoint, not by default."""
        return False
    
    def to_dict(self) -> Dict[str, Any]:
        """Export whiteboard data as dict."""
        return self._data.copy()
    
    def from_dict(self, data: Dict[str, Any]) -> None:
        """Import whiteboard data from dict."""
        self._data = data.copy()
        self.ensure_task_frame()
