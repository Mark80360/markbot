"""AskUserQuestion tool for structured user interaction."""

import asyncio
import uuid
from typing import Any, Awaitable, Callable

from markbot.bus.events import OutboundMessage
from markbot.tools.base import Tool


class AskUserQuestionTool(Tool):
    """Tool to ask users structured questions with predefined options."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        wait_callback: Callable[[str], Awaitable[str]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
    ):
        self._send_callback = send_callback
        self._wait_callback = wait_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._pending_questions: dict[str, asyncio.Future] = {}
        # Session-keyed mapping for channels (e.g. feishu) that cannot
        # propagate question_id metadata on inbound replies.  The loop
        # consults this to route plain-text replies to the pending future
        # without requiring the user to type ``[Q:uuid]`` or the channel
        # to round-trip metadata.
        self._session_questions: dict[str, str] = {}

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id

    @staticmethod
    def _session_key_for_context(context: Any) -> str:
        """Derive the loop-level session_key from a ToolContext.

        Prefers ``context.session_id`` (populated by the iteration runner
        to match ``msg.session_key``); falls back to ``channel:chat_id``.
        """
        if context is None:
            return ""
        sid = getattr(context, "session_id", "") or ""
        if sid:
            return sid
        ch = getattr(context, "channel", "") or ""
        cid = getattr(context, "chat_id", "") or ""
        if ch and cid:
            return f"{ch}:{cid}"
        return ""

    def has_pending(self, session_key: str) -> bool:
        """Return True if ``session_key`` has an unanswered question."""
        return bool(session_key) and session_key in self._session_questions

    def get_pending_qid(self, session_key: str) -> str | None:
        """Return the pending question_id for ``session_key`` (or None)."""
        return self._session_questions.get(session_key) if session_key else None

    def register_pending(self, session_key: str, question_id: str) -> None:
        """Register a pending question for session-keyed reply routing.

        Used by callers that create their own future (e.g. the permission
        approver fallback path) so their replies are also routed by session.
        """
        if session_key and question_id:
            self._session_questions[session_key] = question_id

    def unregister_pending(self, session_key: str) -> None:
        """Drop the session-keyed mapping without resolving the future."""
        self._session_questions.pop(session_key, None)

    def set_callbacks(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]],
        wait_callback: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        """Set the callbacks for sending messages and waiting for responses."""
        self._send_callback = send_callback
        self._wait_callback = wait_callback

    def handle_response(self, question_id: str, response: str) -> None:
        """Handle a response to a pending question."""
        if question_id in self._pending_questions:
            future = self._pending_questions.pop(question_id)
            if not future.done():
                future.set_result(response)
            # Drop the session-keyed mapping so a subsequent message in
            # the same session starts a fresh turn instead of being
            # routed to the now-resolved question.
            for sk, qid in list(self._session_questions.items()):
                if qid == question_id:
                    del self._session_questions[sk]

    @property
    def name(self) -> str:
        return "ask_user_question"

    @property
    def description(self) -> str:
        return "Ask the user a structured question with predefined options. Use this when you need the user to choose between specific alternatives."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user"
                },
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "The option label (e.g., 'Option A')"
                            },
                            "description": {
                                "type": "string",
                                "description": "Optional description of what this option means"
                            }
                        },
                        "required": ["label"]
                    },
                    "description": "List of options for the user to choose from (2-5 options)"
                }
            },
            "required": ["question", "options"]
        }

    async def _legacy_execute(self, **kwargs: Any) -> str:
        question = kwargs.get("question", "")
        options = kwargs.get("options", [])

        if not self._send_callback:
            return "Error: Message sending not configured"

        if not options or len(options) < 2:
            return "Error: Must provide at least 2 options"

        if len(options) > 5:
            return "Error: Maximum 5 options allowed"

        context = kwargs.get("_tool_context")
        ctx_ch = context.channel if context else ""
        ctx_cid = context.chat_id if context else ""

        channel = ctx_ch or self._default_channel
        chat_id = ctx_cid or self._default_chat_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        # Generate unique question ID
        question_id = str(uuid.uuid4())

        # Format question text (no [Q:uuid] — session-keyed routing handles it)
        content = self._format_question(question, options)

        # Send question
        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata={
                "question_id": question_id,
                "question_type": "structured_question",
                "options": options,
            },
        )

        try:
            await self._send_callback(msg)
        except Exception as e:
            return f"Error sending question: {str(e)}"

        # Wait for response
        if self._wait_callback:
            try:
                response = await self._wait_callback(question_id)
                return f"User selected: {response}"
            except Exception as e:
                return f"Error waiting for response: {str(e)}"
        else:
            # Fallback: create future and wait
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._pending_questions[question_id] = future
            session_key = self._session_key_for_context(context)
            if session_key:
                self._session_questions[session_key] = question_id

            try:
                response = await asyncio.wait_for(future, timeout=300.0)  # 5 min timeout
                return f"User selected: {response}"
            except asyncio.TimeoutError:
                self._pending_questions.pop(question_id, None)
                if session_key:
                    self._session_questions.pop(session_key, None)
                return "Error: Question timed out (no response after 5 minutes)"
            except Exception as e:
                self._pending_questions.pop(question_id, None)
                if session_key:
                    self._session_questions.pop(session_key, None)
                return f"Error waiting for response: {str(e)}"
            finally:
                # handle_response() pops both maps on successful resolution
                # and the except blocks pop on timeout/error, but a
                # CancelledError (task aborted via /stop or session reset)
                # bypasses every except branch.  Idempotent pops keep both
                # maps clean regardless of the exit path.
                self._pending_questions.pop(question_id, None)
                if session_key:
                    self._session_questions.pop(session_key, None)

    def _format_question(
        self,
        question: str,
        options: list[dict[str, str]],
    ) -> str:
        """Format question based on channel capabilities."""

        # For channels with interactive capabilities (feishu, dingtalk)
        # the channel handler will use metadata to render interactive cards
        # For other channels, format as text

        lines = [question, ""]
        for i, opt in enumerate(options, 1):
            label = opt.get("label", f"Option {i}")
            desc = opt.get("description", "")
            if desc:
                lines.append(f"{i}. {label} - {desc}")
            else:
                lines.append(f"{i}. {label}")

        lines.append("")
        lines.append("Please reply with the number or label of your choice.")

        return "\n".join(lines)
