"""Interaction logger for AI request/response audit trail.

Records every LLM interaction (messages sent + response received)
to ~/.markbot/logs/YYYY-MM-DD.log for post-hoc analysis and optimization.

Log format: structured text with clear separators, human-readable
but also parseable for automated analysis.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

_MAX_MSG_CONTENT = 3000
_MAX_TOOL_ARGS = 500


class InteractionLogger:
    """Append-only interaction log writer for LLM request/response pairs.

    Each LLM call (each iteration of the agent loop) produces one log entry
    containing:
    - Timestamp and session metadata
    - **Incremental** messages sent to the LLM (only new messages since
      the last logged interaction for this session, avoiding duplicates)
    - Tool definitions summary
    - Complete LLM response (content, tool calls, usage, reasoning)

    Deduplication: system messages and tool definitions are hashed per session.
    When context compaction resets the message counter, unchanged system
    messages and tool defs are replaced with a short ``[unchanged, skipped]``
    marker instead of being re-logged in full.
    """

    def __init__(self, log_dir: Path | None = None):
        if log_dir is None:
            from markbot.config.paths import get_logs_dir
            log_dir = get_logs_dir()
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._logged_msg_counts: dict[str, int] = {}
        self._system_msg_hash: dict[str, str] = {}
        self._tool_defs_hash: dict[str, str] = {}

    def log_interaction(
        self,
        *,
        iteration: int,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]],
        response: Any,
        model: str = "",
        channel: str = "",
        chat_id: str = "",
        tokens_before: int = 0,
    ) -> None:
        """Log one LLM interaction (request + response).

        Only new messages (those not yet logged for this session) are
        written to the request section.  The response section is always
        written in full.  This prevents duplicate content when the same
        conversation history is sent across multiple agent-loop iterations.

        Args:
            iteration: Agent loop iteration number.
            messages: Full message list sent to the LLM.
            tool_defs: Tool definitions sent with the request.
            response: LLMResponse object from the provider.
            model: Model name used for this call.
            channel: Channel identifier.
            chat_id: Chat/session identifier.
            tokens_before: Estimated token count before this call.
        """
        session_key = f"{channel}:{chat_id}"
        prev_count = self._logged_msg_counts.get(session_key, 0)
        compacted = len(messages) < prev_count

        if compacted:
            prev_count = 0

        new_messages = messages[prev_count:]
        self._logged_msg_counts[session_key] = len(messages)

        now = datetime.now()
        filename = f"{now.strftime('%Y-%m-%d')}.log"
        filepath = self._log_dir / filename

        parts: list[str] = []

        parts.append(self._build_header(now, iteration, channel, chat_id, model))
        parts.append(
            self._build_request_section(
                new_messages, tool_defs, tokens_before,
                total_msg_count=len(messages),
                compacted=compacted,
                session_key=session_key,
            )
        )
        parts.append(self._build_response_section(response))

        entry = "\n".join(parts)

        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as e:
            logger.warning("Failed to write to {}: {}", filepath, e)

    def _build_header(
        self,
        ts: datetime,
        iteration: int,
        channel: str,
        chat_id: str,
        model: str,
    ) -> str:
        timestamp = ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        lines = [
            "=" * 80,
            f"INTERACTION #{iteration}  |  {timestamp}",
            f"Channel: {channel or 'N/A'}  |  Chat: {chat_id or 'N/A'}  |  Model: {model or 'N/A'}",
            "=" * 80,
            "",
        ]
        return "\n".join(lines)

    def _build_request_section(
        self,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]],
        tokens_before: int,
        *,
        total_msg_count: int | None = None,
        compacted: bool = False,
        session_key: str = "",
    ) -> str:
        display_total = total_msg_count if total_msg_count is not None else len(messages)
        lines = [
            "--- REQUEST ---",
            f"Messages ({len(messages)} new / {display_total} total), estimated tokens: ~{tokens_before}",
            "",
        ]

        if compacted:
            lines.append("[Context was compacted - previously logged messages summarized]")
            lines.append("")

        for idx, msg in enumerate(messages):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if role == "system" and session_key:
                content_str = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                content_hash = hashlib.md5(content_str.encode()).hexdigest()
                if self._system_msg_hash.get(session_key) == content_hash:
                    lines.append(f"--- [{idx}] role=system [unchanged, content skipped]")
                    lines.append("")
                    continue
                self._system_msg_hash[session_key] = content_hash

            if isinstance(content, list):
                content = self._format_multimodal_content(content)
            elif isinstance(content, str):
                content = self._truncate(content, _MAX_MSG_CONTENT)

            tool_calls = msg.get("tool_calls")
            tc_info = ""
            if tool_calls:
                tc_info = "  [tool_calls: " + ", ".join(
                    tc.get("function", {}).get("name", "?") for tc in tool_calls
                ) + "]"

            lines.append(f"--- [{idx}] role={role}{tc_info}")
            lines.append(content)
            lines.append("")

        if session_key and tool_defs:
            tool_defs_str = json.dumps(tool_defs, ensure_ascii=False, sort_keys=True)
            tool_defs_hash = hashlib.md5(tool_defs_str.encode()).hexdigest()
            if self._tool_defs_hash.get(session_key) == tool_defs_hash:
                lines.append(f"[Tool Definitions ({len(tool_defs)})] [unchanged, skipped]")
            else:
                self._tool_defs_hash[session_key] = tool_defs_hash
                tool_names = []
                for td in tool_defs:
                    fn = td.get("function", {})
                    name = fn.get("name", "?")
                    desc = fn.get("description", "")
                    tool_names.append(f"  - {name}: {desc}")
                lines.append(f"[Tool Definitions ({len(tool_defs)})]")
                lines.extend(tool_names)
        else:
            tool_names = []
            for td in tool_defs:
                fn = td.get("function", {})
                name = fn.get("name", "?")
                desc = fn.get("description", "")
                tool_names.append(f"  - {name}: {desc}")
            lines.append(f"[Tool Definitions ({len(tool_defs)})]")
            lines.extend(tool_names)

        if not tool_defs:
            lines.append("  (none)")
        lines.append("")
        lines.append("--- END REQUEST ---")
        lines.append("")
        return "\n".join(lines)

    def _build_response_section(self, response: Any) -> str:
        try:
            finish_reason = getattr(response, "finish_reason", "N/A")
            content = getattr(response, "content", None) or ""
            has_tool_calls = getattr(response, "has_tool_calls", False)
            tool_calls = getattr(response, "tool_calls", []) or []
            usage = getattr(response, "usage", None) or {}
            reasoning_content = getattr(response, "reasoning_content", None)
            thinking_blocks = getattr(response, "thinking_blocks", None) or []
        except Exception:
            return "--- RESPONSE ---\n(unable to parse response)\n--- END RESPONSE ---\n\n"

        lines = ["--- RESPONSE ---"]
        lines.append(f"finish_reason: {finish_reason}")

        if content:
            lines.append("")
            lines.append("[Content]")
            lines.append(self._truncate(content, _MAX_MSG_CONTENT))

        if reasoning_content:
            lines.append("")
            lines.append("[Reasoning Content]")
            lines.append(self._truncate(reasoning_content, _MAX_MSG_CONTENT))

        if thinking_blocks:
            lines.append("")
            lines.append(f"[Thinking Blocks ({len(thinking_blocks)})]")
            for i, block in enumerate(thinking_blocks[:3]):
                block_text = json.dumps(block, ensure_ascii=False)[:500]
                lines.append(f"  Block {i}: {block_text}")
            if len(thinking_blocks) > 3:
                lines.append(f"  ... and {len(thinking_blocks) - 3} more blocks")

        if has_tool_calls and tool_calls:
            lines.append("")
            lines.append(f"[Tool Calls ({len(tool_calls)})]")
            for tc in tool_calls:
                name = getattr(tc, "name", "?")
                args = getattr(tc, "arguments", {})
                if isinstance(args, dict):
                    args_str = json.dumps(args, ensure_ascii=False)
                else:
                    args_str = str(args)
                lines.append(f"  -> {name}({self._truncate(args_str, _MAX_TOOL_ARGS)})")

        if usage:
            lines.append("")
            lines.append("[Usage]")
            for k, v in usage.items():
                lines.append(f"  {k}: {v}")

        lines.append("")
        lines.append("--- END RESPONSE ---")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        half = max_len // 2
        return (
            text[:half]
            + f"\n\n... [truncated: {len(text)} chars total] ...\n\n"
            + text[-half:]
        )

    @staticmethod
    def _format_multimodal_content(parts: list) -> str:
        lines: list[str] = []
        for part in parts:
            ptype = part.get("type", "?")
            if ptype == "text":
                text = part.get("text", "")
                if text is None:
                    text = ""
                lines.append(text)
            elif ptype == "image_url":
                url = part.get("image_url", {}).get("url", "")[:100]
                lines.append(f"[image: {url}...]")
            else:
                lines.append(f"[{ptype}]")
        return "\n".join(lines)
