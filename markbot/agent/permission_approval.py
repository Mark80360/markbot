"""Interactive permission approval for tool ``ask`` decisions.

Routes confirmation requests through the same channel/chat that originated
the turn, reusing AskUserQuestionTool's pending-response machinery when
available, and falling back to a lightweight outbound message + future.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable

from loguru import logger

from markbot.bus.events import OutboundMessage
from markbot.types.tool import ToolContext


class PermissionApprover:
    """Ask the user to approve or deny a tool call."""

    def __init__(
        self,
        *,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        question_tool: Any = None,
        timeout_s: float = 300.0,
    ) -> None:
        self._send_callback = send_callback
        self._question_tool = question_tool
        self._timeout_s = timeout_s
        self._pending: dict[str, asyncio.Future[str]] = {}

    def set_send_callback(
        self, callback: Callable[[OutboundMessage], Awaitable[None]] | None
    ) -> None:
        self._send_callback = callback

    def set_question_tool(self, tool: Any) -> None:
        self._question_tool = tool

    def handle_response(self, approval_id: str, response: str) -> bool:
        """Resolve a pending approval if ``approval_id`` is known."""
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result(response)
        self._pending.pop(approval_id, None)
        return True

    async def __call__(
        self,
        tool_name: str,
        params: dict[str, Any],
        context: ToolContext,
        reason: str,
    ) -> bool:
        channel = context.channel or ""
        chat_id = context.chat_id or ""
        if not channel or not chat_id:
            logger.warning(
                "Permission approval requested for {} without channel/chat", tool_name
            )
            return False

        preview = _params_preview(params)
        prompt = (
            f"Permission required for tool `{tool_name}`.\n"
            f"Reason: {reason}\n"
            f"Args: {preview}\n\n"
            "Approve this action?"
        )
        options = [
            {"label": "Allow", "description": f"Run {tool_name} once"},
            {"label": "Deny", "description": "Do not run this tool"},
        ]

        # CLI can approve via stdin without a concurrent inbound bus consumer
        # (important for ``markbot agent -m`` / process_direct).
        if channel in {"cli", "web"} and _can_prompt_stdin():
            try:
                return await _prompt_stdin(prompt)
            except Exception as exc:
                logger.warning("stdin approval failed for {}: {}", tool_name, exc)

        # Prefer the shared question tool so channel cards / middleware work.
        if self._question_tool is not None and getattr(
            self._question_tool, "_send_callback", None
        ):
            try:
                self._question_tool.set_context(channel, chat_id)
                result = await self._question_tool.execute(
                    {"question": prompt, "options": options},
                    context,
                )
                return _is_allow(str(result))
            except Exception as exc:
                logger.warning(
                    "Question-tool approval failed for {}, falling back: {}",
                    tool_name,
                    exc,
                )

        if self._send_callback is None:
            return False

        approval_id = str(uuid.uuid4())
        content = (
            f"{prompt}\n\n"
            f"1. Allow\n2. Deny\n\n"
            f"Reply with Allow/Deny (or 1/2). [Q:{approval_id}]"
        )
        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata={
                "question_id": approval_id,
                "question_type": "permission_approval",
                "options": options,
                "tool_name": tool_name,
            },
        )
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = future
        # Also register on question tool so existing middleware can resolve it.
        if self._question_tool is not None and hasattr(
            self._question_tool, "_pending_questions"
        ):
            self._question_tool._pending_questions[approval_id] = future
        try:
            await self._send_callback(msg)
            response = await asyncio.wait_for(future, timeout=self._timeout_s)
            return _is_allow(response)
        except asyncio.TimeoutError:
            logger.warning("Permission approval timed out for {}", tool_name)
            return False
        except Exception as exc:
            logger.error("Permission approval error for {}: {}", tool_name, exc)
            return False
        finally:
            self._pending.pop(approval_id, None)
            if self._question_tool is not None and hasattr(
                self._question_tool, "_pending_questions"
            ):
                self._question_tool._pending_questions.pop(approval_id, None)


def _can_prompt_stdin() -> bool:
    try:
        import sys

        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


async def _prompt_stdin(prompt: str) -> bool:
    """Blocking stdin prompt offloaded to a worker thread."""
    import sys

    def _ask() -> str:
        sys.stdout.write(prompt + "\n[Allow/Deny]: ")
        sys.stdout.flush()
        return sys.stdin.readline()

    response = await asyncio.to_thread(_ask)
    return _is_allow(response or "")


def _params_preview(params: dict[str, Any], limit: int = 240) -> str:
    try:
        text = json.dumps(params, ensure_ascii=False, default=str)
    except Exception:
        text = str(params)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _is_allow(response: str) -> bool:
    text = (response or "").strip().lower()
    # Handle "User selected: Allow" from AskUserQuestionTool
    if "user selected:" in text:
        text = text.split("user selected:", 1)[1].strip()
    # Strip option numbering / descriptions
    text = text.split("-", 1)[0].strip()
    text = text.split(".", 1)[0].strip() if text[:1].isdigit() else text
    if text in {"1", "y", "yes", "allow", "approve", "ok", "true", "a"}:
        return True
    if text.startswith("allow") or text.startswith("approve"):
        return True
    return False
