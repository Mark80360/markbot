"""Turn lifecycle management: tombstone mechanism for detecting incomplete turns.

Extracted from AgentLoop to decouple turn tracking from the main message loop.
"""

from datetime import datetime, timedelta
from uuid import uuid4

from loguru import logger

from markbot.bus.events import InboundMessage
from markbot.session.manager import Session


class TurnLifecycle:
    """Manages turn lifecycle with tombstone markers for crash recovery.

    The tombstone pattern ensures that if a turn fails or the process crashes,
    the next incoming message can detect and handle the incomplete state.
    """

    _CONTINUE_INDICATORS = [
        "继续", "continue", "go on", "more", "接着",
        "说下去", "完成", "finish", "kontynuuj",
        "fortsetzen", "continuer",
    ]

    def begin_turn(self, session: Session, user_input: str) -> str:
        """Set tombstone marker for a new turn.

        Overwrites any previous incomplete turn marker.

        Args:
            session: Current session
            user_input: User's input content

        Returns:
            turn_id: Unique identifier for this turn
        """
        turn_id = str(uuid4())[:8]
        session.metadata["active_turn"] = {
            "turn_id": turn_id,
            "user_input": user_input,
            "started_at": datetime.now().isoformat(),
            "status": "running",
            "original_history_len": len(session.get_history()),
        }
        logger.debug(f"[Tombstone] Set turn {turn_id} for session {session.key}")
        return turn_id

    def complete_turn(self, session: Session, turn_id: str) -> None:
        """Clear tombstone marker, indicating successful completion.

        Args:
            session: Current session
            turn_id: Turn ID to clear (for verification)
        """
        active = session.metadata.get("active_turn")
        if active and active.get("turn_id") == turn_id:
            del session.metadata["active_turn"]
            logger.debug(f"[Tombstone] Cleared turn {turn_id} for session {session.key}")

    def fail_turn(self, session: Session, turn_id: str, error: str) -> None:
        """Mark a turn as failed.

        Args:
            session: Current session
            turn_id: Failed turn ID
            error: Error message
        """
        active = session.metadata.get("active_turn")
        if active and active.get("turn_id") == turn_id:
            active["status"] = "failed"
            active["error"] = error
            active["failed_at"] = datetime.now().isoformat()
            logger.warning(f"[Tombstone] Marked turn {turn_id} as failed: {error}")

    def check_incomplete(self, session: Session, msg: InboundMessage) -> str | None:
        """Check for incomplete turns and generate context hint.

        When a new message arrives, checks if there was an unfinished turn.
        Returns a context note to inject into the conversation if needed.

        Args:
            session: Current session
            msg: New inbound message

        Returns:
            Context note string if recovery needed, else None
        """
        active = session.metadata.get("active_turn")
        if not active:
            return None

        user_msg_clean = msg.content.strip()
        is_likely_continue = (
            len(user_msg_clean) <= 20
            and any(ind in user_msg_clean.lower() for ind in self._CONTINUE_INDICATORS)
        )

        status = active.get("status", "unknown")
        previous_input = active.get("user_input", "")

        if status == "failed":
            context_note = (
                f"[系统提示：上一轮对话在处理用户请求时异常中断。\n"
                f"之前的请求：'{previous_input}'\n"
                f"由于对话未正常完成，请注意上下文可能不完整。"
            )
            if is_likely_continue:
                context_note += "用户很可能希望继续完成之前的任务。]"
            else:
                context_note += "]"
            logger.info(
                f"[Tombstone] Detected incomplete turn with failed status for session {session.key}"
            )
            return context_note

        elif status == "running":
            context_note = (
                f"[系统提示：检测到未完成的对话轮次（可能是程序异常退出导致）。\n"
                f"上一轮用户请求：'{previous_input}'\n"
                f"状态：未完成（status={status}）"
            )
            if is_likely_continue:
                context_note += "用户当前简短的回复可能与此请求相关。]"
            else:
                context_note += "]"
            logger.info(
                f"[Tombstone] Detected incomplete turn with running status for session {session.key}"
            )
            return context_note

        return None

    def cleanup_stale(self, session: Session, max_age_hours: int = 24) -> None:
        """Remove expired tombstone markers.

        Args:
            session: Current session
            max_age_hours: Maximum retention time in hours (default 24)
        """
        active = session.metadata.get("active_turn")
        if not active:
            return

        try:
            started = datetime.fromisoformat(active["started_at"])
            if datetime.now() - started > timedelta(hours=max_age_hours):
                logger.info(
                    f"[Tombstone] Auto-cleaning stale tombstone from "
                    f"{active['started_at']} for session {session.key}"
                )
                del session.metadata["active_turn"]
        except (KeyError, ValueError, TypeError):
            if "active_turn" in session.metadata:
                del session.metadata["active_turn"]

    @property
    def has_active_turn(self, session: Session) -> bool:
        """Check if session has an active (incomplete) turn."""
        return "active_turn" in session.metadata
