"""L1 Whiteboard Memory - Loop-level temporary workspace.

Provides structured task state management for a single Agent Loop execution.
Supports checkpoint persistence for resumable loops.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import (
    BaseMemoryLayer, MemoryTier, LoopState,
    ExecutionStep, SubtaskRecord
)


class WhiteboardMemory(BaseMemoryLayer):
    """
    L1 Whiteboard: Loop-level temporary workspace.
    
    Lifecycle:
    - Only lives during a single Agent Loop execution
    - Cleared automatically after loop ends (unless failed)
    - Supports checkpoint for resumable loops
    
    Purpose:
    - Track task state and execution progress
    - Store intermediate results and notes
    - Support DAG-based dependency tracking for subtasks
    
    Storage: In-memory with optional JSON checkpoint persistence
    """
    
    MAX_FIELD_SIZE = 1024          # Max size per field (1KB)
    MAX_TOTAL_SIZE = 102400        # Max total whiteboard size (100KB)
    
    def __init__(self, workspace_path: str, chat_id: str):
        super().__init__(MemoryTier.WHITEBOARD, workspace_path)
        self.chat_id = chat_id
        self._data: Dict[str, Any] = {}
        self._version = "2.0"
        
        # Checkpoint path for resumable loops
        self._checkpoint_path = Path(workspace_path) / "memory" / "checkpoints" / f"{chat_id}.json"
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._init_default_structure()
        self._initialized = True
    
    def _init_default_structure(self) -> None:
        """Initialize default whiteboard structure with version control."""
        self._data = {
            "_version": self._version,
            "_created_at": time.time(),
            "_last_updated": time.time(),
            
            # Core task tracking
            "task_specification": "",      # Natural language description
            "execution_plan": [],           # List of ExecutionStep dicts
            "current_state": LoopState.INITIAL.value,
            "loop_counter": 0,
            
            # Subtask management
            "completed_subtasks": [],       # List of SubtaskRecord dicts
            "pending_subtasks": [],         # List of pending task strings
            
            # Result caching
            "intermediate_results": {},     # Key-value cache
            
            # Execution metadata
            "notes": [],                    # Timestamped notes list
            "content_registry": {},         # Content processing registry
            "error_log": [],                # Error history for this loop
            
            # Performance metrics
            "tool_call_count": 0,
            "total_tokens_used": 0,
            "start_time": None,
            "end_time": None
        }
    
    def update(self, key: str, value: Any) -> bool:
        """
        Update a whiteboard field with validation.
        
        Args:
            key: Field name
            value: New value (must be JSON-serializable)
            
        Returns:
            True if update succeeded, False if validation failed
        """
        # Validate field size
        try:
            value_str = json.dumps(value, ensure_ascii=False)
            if len(value_str) > self.MAX_FIELD_SIZE:
                logger.warning(
                    f"[Whiteboard] Field '{key}' exceeds max size "
                    f"({len(value_str)} > {self.MAX_FIELD_SIZE})"
                )
                return False
        except (TypeError, ValueError):
            logger.warning(f"[Whiteboard] Field '{key}' value is not JSON-serializable")
            return False
        
        # Validate total size
        self._data[key] = value
        self._data["_last_updated"] = time.time()
        
        # Auto-increment loop counter on state changes
        if key == "current_state":
            self._data["loop_counter"] = self._data.get("loop_counter", 0) + 1
        
        return True
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a whiteboard field value."""
        return self._data.get(key, default)
    
    def add_note(self, note: str) -> None:
        """Add a timestamped note to the whiteboard."""
        timestamp = time.strftime("%H:%M:%S")
        self._data["notes"].append({
            "timestamp": timestamp,
            "note": note,
            "epoch": time.time()
        })
        self._data["_last_updated"] = time.time()
    
    def update_turn_context(
        self, 
        turn_number: int, 
        user_input: str, 
        assistant_response: str
    ) -> None:
        """
        Update whiteboard with latest turn context.
        
        Called by MemoryManager during process_turn() to keep
        the whiteboard synchronized with conversation progress.
        
        Args:
            turn_number: Current turn number
            user_input: User's message for this turn
            assistant_response: Assistant's response for this turn
        """
        self._data["current_turn"] = turn_number
        self._data["last_user_input"] = user_input[:500]  # Truncate very long inputs
        self._data["last_assistant_response"] = assistant_response[:500]
        self._data["_last_updated"] = time.time()
    
    def update_state(self, new_state: Union[str, LoopState]) -> bool:
        """
        Update the current loop execution state.
        
        Args:
            new_state: New state (string or LoopState enum)
            
        Returns:
            True if update succeeded
        """
        if isinstance(new_state, LoopState):
            state_value = new_state.value
        else:
            state_value = str(new_state)
        
        return self.update("current_state", state_value)
    
    def add_error(self, error: str, context: Optional[str] = None) -> None:
        """Log an error to the error history."""
        self._data["error_log"].append({
            "timestamp": time.strftime("%H:%M:%S"),
            "error": error,
            "context": context,
            "epoch": time.time()
        })
    
    def add_completed_subtask(self, subtask: str, result: str) -> None:
        """Mark a subtask as completed with result."""
        record = SubtaskRecord(task=subtask, result=result)
        self._data["completed_subtasks"].append(record.to_dict())
        
        # Remove from pending if present
        if subtask in self._data["pending_subtasks"]:
            self._data["pending_subtasks"].remove(subtask)
        
        self._data["_last_updated"] = time.time()
    
    def add_pending_subtask(self, subtask: str) -> None:
        """Add a subtask to the pending list."""
        if subtask not in self._data["pending_subtasks"]:
            self._data["pending_subtasks"].append(subtask)
            self._data["_last_updated"] = time.time()
    
    def store_result(self, key: str, result: Any) -> bool:
        """
        Store an intermediate result in the cache.
        
        Args:
            key: Result identifier
            result: Cached value (must be JSON-serializable)
            
        Returns:
            True if stored successfully
        """
        try:
            # Validate serializability and size
            result_str = json.dumps(result, ensure_ascii=False)
            if len(result_str) > self.MAX_FIELD_SIZE:
                logger.warning(f"[Whiteboard] Result '{key}' too large, skipping")
                return False
            
            self._data["intermediate_results"][key] = result
            self._data["_last_updated"] = time.time()
            return True
        except (TypeError, ValueError) as e:
            logger.warning(f"[Whiteboard] Cannot serialize result '{key}': {e}")
            return False
    
    def get_result(self, key: str) -> Any:
        """Get a cached intermediate result."""
        return self._data.get("intermediate_results", {}).get(key)
    
    def add_execution_step(
        self, 
        step_id: Union[str, ExecutionStep], 
        description: str = "", 
        status: str = "PENDING",
        dependencies: Optional[List[str]] = None
    ) -> None:
        """
        Add a step to the execution plan.
        
        Can accept either:
        - An ExecutionStep object directly
        - Individual parameters (step_id, description, etc.)
        """
        if isinstance(step_id, ExecutionStep):
            step = step_id
        else:
            step = ExecutionStep(
                step_id=step_id,
                description=description,
                status=status,
                dependencies=dependencies or []
            )
        self._data["execution_plan"].append(step.to_dict())
        self._data["_last_updated"] = time.time()
    
    def update_step_status(
        self, 
        step_id: str, 
        status: str, 
        result: Optional[str] = None
    ) -> bool:
        """
        Update the status of an execution step.
        
        Args:
            step_id: Step identifier
            status: New status (PENDING/IN_PROGRESS/COMPLETED/FAILED)
            result: Optional result text
            
        Returns:
            True if step was found and updated
        """
        for step in self._data["execution_plan"]:
            if step.get("step_id") == step_id:
                step["status"] = status
                if result is not None:
                    step["result"] = result
                self._data["_last_updated"] = time.time()
                return True
        return False
    
    def ensure_task_frame(self) -> None:
        """Ensure all required fields exist (called at loop start)."""
        required_keys = [
            "task_specification",
            "execution_plan", 
            "current_state",
            "loop_counter",
            "completed_subtasks",
            "pending_subtasks",
            "intermediate_results",
            "content_registry",
            "notes"
        ]
        
        for key in required_keys:
            if key not in self._data:
                if key in ["execution_plan", "completed_subtasks", "pending_subtasks", "notes", "error_log"]:
                    self._data[key] = []
                elif key == "current_state":
                    self._data[key] = LoopState.INITIAL.value
                elif key == "loop_counter":
                    self._data[key] = 0
                else:
                    self._data[key] = {} if key != "task_specification" else ""
    
    def register_content(self, content_id: str, content_type: str, path: str) -> None:
        """Register processed content to avoid reprocessing."""
        self._data["content_registry"][content_id] = {
            "type": content_type,
            "path": path,
            "processed_at": time.time()
        }
    
    def is_content_registered(self, content_id: str) -> bool:
        """Check if content has already been processed."""
        return content_id in self._data.get("content_registry", {})
    
    def increment_tool_count(self) -> None:
        """Increment tool call counter."""
        self._data["tool_call_count"] = self._data.get("tool_call_count", 0) + 1
    
    def record_token_usage(self, tokens: int) -> None:
        """Record token usage for this loop."""
        self._data["total_tokens_used"] = (
            self._data.get("total_tokens_used", 0) + tokens
        )
    
    def set_timing(self, start: Optional[bool] = True) -> None:
        """Record start or end time."""
        if start:
            self._data["start_time"] = time.time()
        else:
            self._data["end_time"] = time.time()
    
    def get_duration(self) -> Optional[float]:
        """Get loop duration in seconds."""
        if self._data.get("start_time") and self._data.get("end_time"):
            return self._data["end_time"] - self._data["start_time"]
        return None
    
    # Checkpoint persistence methods
    
    def save_checkpoint(self) -> bool:
        """
        Save current state to checkpoint file for recovery.
        
        Returns:
            True if saved successfully
        """
        try:
            checkpoint = {
                "chat_id": self.chat_id,
                "timestamp": time.time(),
                "version": self._version,
                "data": self._data
            }
            
            self._checkpoint_path.write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            logger.info(f"[Whiteboard] Checkpoint saved for {self.chat_id}")
            return True
        except Exception as e:
            logger.error(f"[Whiteboard] Failed to save checkpoint: {e}")
            return False
    
    def load_checkpoint(self) -> bool:
        """
        Load state from checkpoint file.
        
        Returns:
            True if loaded successfully
        """
        if not self._checkpoint_path.exists():
            return False
        
        try:
            checkpoint = json.loads(
                self._checkpoint_path.read_text(encoding="utf-8")
            )
            
            # Validate chat ID matches
            if checkpoint.get("chat_id") != self.chat_id:
                logger.warning(
                    f"[Whiteboard] Checkpoint chat ID mismatch: "
                    f"{checkpoint.get('chat_id')} != {self.chat_id}"
                )
                return False
            
            # Validate version compatibility
            ckpt_version = checkpoint.get("version", "1.0")
            if ckpt_version != self._version:
                logger.info(
                    f"[Whiteboard] Loading checkpoint from version {ckpt_version}"
                )
            
            self._data = checkpoint.get("data", {})
            self.ensure_task_frame()
            
            logger.info(
                f"[Whiteboard] Restored checkpoint for {self.chat_id} "
                f"(state={self._data.get('current_state')}, "
                f"steps_done={len(self._data.get('completed_subtasks', []))})"
            )
            return True
            
        except Exception as e:
            logger.error(f"[Whiteboard] Failed to load checkpoint: {e}")
            return False
    
    def clear_checkpoint(self) -> None:
        """Remove checkpoint file after successful completion."""
        if self._checkpoint_path.exists():
            try:
                self._checkpoint_path.unlink()
                logger.debug(f"[Whiteboard] Checkpoint cleared for {self.chat_id}")
            except Exception as e:
                logger.warning(f"[Whiteboard] Failed to clear checkpoint: {e}")
    
    # BaseMemoryLayer interface implementation
    
    def add(self, content: str, **metadata) -> bool:
        """Add content as a note."""
        self.add_note(content)
        return True
    
    def get_context(self, query: Optional[str] = None, limit: int = 10) -> str:
        """
        Get whiteboard context formatted for prompt injection.
        
        Returns structured markdown with current state and key information.
        """
        lines = ["## 📋 Whiteboard Context (L1 - Current Loop State)"]
        
        # Basic state info
        lines.append(f"\n**Status**: `{self._data.get('current_state', 'UNKNOWN')}`")
        lines.append(f"**Loop**: #{self._data.get('loop_counter', 0)}")
        
        # Task specification
        if self._data.get("task_specification"):
            lines.append(f"\n**Task**:\n{self._data['task_specification']}")
        
        # Pending subtasks
        pending = self._data.get("pending_subtasks", [])
        if pending:
            lines.append("\n### ⏳ Pending Subtasks:")
            for task in pending[-limit:]:
                lines.append(f"- [ ] {task}")
        
        # Completed subtasks (show last few)
        completed = self._data.get("completed_subtasks", [])
        if completed:
            lines.append("\n### ✅ Recently Completed:")
            for record in completed[-limit:]:
                lines.append(f"- [x] {record.get('task', 'unknown')}: {record.get('result', '')[:100]}")
        
        # Recent notes (if any)
        notes = self._data.get("notes", [])
        if notes:
            lines.append("\n### 📝 Recent Notes:")
            for note in notes[-3:]:
                lines.append(f"- **{note.get('timestamp', '')}**: {note.get('note', '')}")
        
        # Error summary (if any)
        errors = self._data.get("error_log", [])
        if errors:
            lines.append("\n### ⚠️ Recent Errors:")
            for error in errors[-3:]:
                lines.append(f"- **{error.get('timestamp', '')}**: {error.get('error', '')[:150]}")
        
        # Metrics
        tool_calls = self._data.get("tool_call_count", 0)
        tokens = self._data.get("total_tokens_used", 0)
        duration = self.get_duration()
        
        if tool_calls or tokens or duration:
            lines.append("\n### 📊 Metrics:")
            if tool_calls:
                lines.append(f"- Tool calls: {tool_calls}")
            if tokens:
                lines.append(f"- Tokens used: {tokens:,}")
            if duration:
                lines.append(f"- Duration: {duration:.1f}s")
        
        return "\n".join(lines)
    
    def clear(self) -> None:
        """Clear all whiteboard data."""
        self._init_default_structure()
        self.clear_checkpoint()
        logger.debug(f"[Whiteboard] Cleared for {self.chat_id}")
    
    @property
    def is_persistent(self) -> bool:
        """Whiteboard is not persistent by design (temporary)."""
        return False
    
    @property
    def data_size(self) -> int:
        """Get current data size in bytes."""
        try:
            return len(json.dumps(self._data, ensure_ascii=False))
        except Exception:
            return 0
    
    def to_dict(self) -> Dict[str, Any]:
        """Export whiteboard state as dictionary."""
        return {
            "chat_id": self.chat_id,
            "version": self._version,
            "current_state": self._data.get("current_state"),
            "loop_counter": self._data.get("loop_counter", 0),
            "pending_tasks": len(self._data.get("pending_subtasks", [])),
            "completed_tasks": len(self._data.get("completed_subtasks", [])),
            "notes_count": len(self._data.get("notes", [])),
            "errors_count": len(self._data.get("error_log", [])),
            "tool_calls": self._data.get("tool_call_count", 0),
            "data_size_bytes": self.data_size,
            "has_checkpoint": self._checkpoint_path.exists()
        }
    
    def read_all(self) -> Dict[str, Any]:
        """
        Read all whiteboard data.
        
        Returns complete internal data dictionary including
        task_specification, notes, execution_plan, etc.
        """
        return dict(self._data)
