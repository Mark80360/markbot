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
from markbot.locales import t
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
        # Sessions where the user chose "Allow All" — subsequent tool
        # approvals in the same turn are auto-approved without prompting.
        # Cleared by the loop when the turn (session lock holder) ends.
        self._allow_all_sessions: set[str] = set()

    def set_send_callback(
        self, callback: Callable[[OutboundMessage], Awaitable[None]] | None
    ) -> None:
        self._send_callback = callback

    def set_question_tool(self, tool: Any) -> None:
        self._question_tool = tool

    def is_allow_all(self, session_key: str) -> bool:
        """Return True if the user chose "Allow All" for this session."""
        return session_key in self._allow_all_sessions

    def set_allow_all(self, session_key: str) -> None:
        """Mark this session as auto-approve for the rest of the turn."""
        self._allow_all_sessions.add(session_key)

    def clear_allow_all(self, session_key: str) -> None:
        """Clear "Allow All" — called when the turn ends."""
        self._allow_all_sessions.discard(session_key)

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

        session_key = getattr(context, "session_id", "") or ""
        if not session_key:
            session_key = f"{channel}:{chat_id}"

        # "Allow All" — auto-approve without prompting.
        if session_key in self._allow_all_sessions:
            logger.info(
                "Auto-approving {} (allow-all for session {})", tool_name, session_key
            )
            return True

        prompt = _build_prompt(tool_name, params, reason)
        options = [
            {"label": t("permission.opt_allow_label"), "description": t("permission.opt_allow_desc", tool_name=tool_name)},
            {"label": t("permission.opt_allow_all_label"), "description": t("permission.opt_allow_all_desc")},
            {"label": t("permission.opt_deny_label"), "description": t("permission.opt_deny_desc")},
        ]

        # CLI can approve via stdin without a concurrent inbound bus consumer
        # (important for ``markbot agent -m`` / process_direct).
        if channel in {"cli", "web"} and _can_prompt_stdin():
            try:
                full_prompt = _format_full_prompt(prompt, tool_name)
                choice = await _prompt_stdin(full_prompt)
                return _apply_choice(choice, self, session_key)
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
                choice = _parse_choice(str(result))
                return _apply_choice(choice, self, session_key)
            except Exception as exc:
                logger.warning(
                    "Question-tool approval failed for {}, falling back: {}",
                    tool_name,
                    exc,
                )

        if self._send_callback is None:
            return False

        approval_id = str(uuid.uuid4())
        full_prompt = _format_full_prompt(prompt, tool_name)
        content = _format_fallback(full_prompt, approval_id)
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
            if hasattr(self._question_tool, "register_pending"):
                self._question_tool.register_pending(session_key, approval_id)
        try:
            await self._send_callback(msg)
            response = await asyncio.wait_for(future, timeout=self._timeout_s)
            choice = _parse_choice(response)
            return _apply_choice(choice, self, session_key)
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
            if self._question_tool is not None and hasattr(
                self._question_tool, "unregister_pending"
            ):
                self._question_tool.unregister_pending(session_key)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _build_prompt(tool_name: str, params: dict[str, Any], reason: str) -> str:
    """Build the header portion of a permission prompt (no options).

    The options listing is appended by ``_format_question`` (question-tool
    path) or by ``_format_full_prompt`` (stdin / fallback path) to avoid
    rendering them twice.
    """
    args_text = _format_args(params)
    lines = [
        t("permission.confirmation_header"),
        "",
        t("permission.tool_label", tool_name=tool_name),
        t("permission.reason_label", reason=reason),
        "",
        t("permission.arguments_label"),
        args_text,
    ]
    return "\n".join(lines)


def _format_full_prompt(header: str, tool_name: str) -> str:
    """Append the options listing to ``header`` for stdin / fallback paths."""
    return "\n".join([
        header,
        "",
        t("permission.choose_option"),
        t("permission.option_allow", tool_name=tool_name),
        t("permission.option_allow_all"),
        t("permission.option_deny"),
        "",
        t("permission.reply_hint"),
    ])


def _format_args(params: dict[str, Any], max_value_len: int = 80) -> str:
    """Format tool parameters as concise single-line key-value pairs.

    Long and multi-line values are truncated to the first line so the
    prompt stays scannable — the user just needs enough context to
    decide allow/deny, not the full file contents.
    """
    if not params:
        return t("permission.args_none")
    lines: list[str] = []
    for key, value in params.items():
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value) if value is not None else ""
        text = text.replace("\\n", "\n").strip()
        # For multi-line values, keep only the first line + a line count hint.
        if "\n" in text:
            first_line = text.split("\n", 1)[0].strip()
            n_lines = text.count("\n") + 1
            text = f"{first_line} … ({n_lines} lines)" if first_line else f"… ({n_lines} lines)"
        if len(text) > max_value_len:
            text = text[:max_value_len].rstrip() + " …"
        lines.append(f"  {key}: {text}")
    return "\n".join(lines)


def _format_fallback(prompt: str, approval_id: str) -> str:
    """Format the fallback (no question_tool) approval message.

    The ``[Q:uuid]`` marker is kept here ONLY because the fallback path
    does not go through ``_format_question``; the loop's session-keyed
    routing makes it unnecessary for reply matching, but it's retained
    as a defensive fallback for the content-prefix matching strategy.
    """
    return f"{prompt}\n[Q:{approval_id}]"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_choice(response: str) -> str:
    """Parse an approval response.

    Returns ``"allow"``, ``"allow_all"``, or ``"deny"``.

    Recognizes the localized option labels (e.g. ``"允许"`` / ``"全部允许"``
    / ``"拒绝"`` when display language is Chinese) so a non-English UI
    still routes correctly.  English keywords (``allow`` / ``deny`` /
    ``allow all``) and bare numbers (1/2/3) are always accepted as a
    fallback, regardless of the active language.
    """
    text = (response or "").strip().lower()
    # Strip "User selected:" prefix from AskUserQuestionTool.
    if "user selected:" in text:
        text = text.split("user selected:", 1)[1].strip()
    # Strip [Q:uuid] suffix if present.
    if "[q:" in text:
        text = text.split("[q:", 1)[0].strip()

    # Localized label matching — check the current language's option labels
    # first so a user who clicks "允许" (zh) is correctly routed to "allow".
    # Order matters: check "allow_all" before "allow" because the
    # allow-all label may contain the allow label as a substring.
    allow_all_label = t("permission.opt_allow_all_label").lower()
    deny_label = t("permission.opt_deny_label").lower()
    allow_label = t("permission.opt_allow_label").lower()
    if allow_all_label and allow_all_label in text:
        return "allow_all"
    if deny_label and deny_label in text:
        return "deny"
    if allow_label and allow_label in text:
        return "allow"

    # English keyword fallback (always accepted regardless of UI language).
    if "allow all" in text or "allow-all" in text or text.strip() in {"2", "all"}:
        return "allow_all"
    if "deny" in text or text.startswith("no") or text.startswith("cancel"):
        return "deny"
    if "allow" in text or "approve" in text:
        return "allow"
    # Bare number / keyword matching.
    parts = text.split()
    first_word = parts[0].rstrip(".,)") if parts else ""
    if first_word in {"1", "y", "yes", "ok", "true", "a"}:
        return "allow"
    if first_word in {"3", "n", "no", "false", "d"}:
        return "deny"
    # Unknown → safe default.
    return "deny"


def _apply_choice(choice: str, approver: PermissionApprover, session_key: str) -> bool:
    """Apply a parsed choice and return whether the tool is approved."""
    if choice == "allow_all":
        approver.set_allow_all(session_key)
        return True
    if choice == "allow":
        return True
    return False


# ---------------------------------------------------------------------------
# Stdin helpers
# ---------------------------------------------------------------------------

def _can_prompt_stdin() -> bool:
    try:
        import sys

        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


async def _prompt_stdin(prompt: str) -> str:
    """Blocking stdin prompt offloaded to a worker thread.

    Returns the parsed choice string (``"allow"``, ``"allow_all"``, ``"deny"``).
    """
    import sys

    def _ask() -> str:
        sys.stdout.write(prompt + "\n> ")
        sys.stdout.flush()
        return sys.stdin.readline()

    response = await asyncio.to_thread(_ask)
    return _parse_choice(response or "")
