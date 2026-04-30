"""Multi-level conversation context compression service.

4-tier progressive compression strategy (inspired by Claude's context management):
  Level 1: Context Collapse  — Truncate individual tool_result blocks that exceed char limit
  Level 2: Micro-Compact      — Remove old tool_result content, keep recent N turns
  Level 3: Auto-Compaction    — LLM generates summary to replace old history
  Level 4: History Snip       — Force-drop oldest messages (last resort)

Each level is progressively more aggressive and expensive.
The system tries the cheapest level first and escalates only when needed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from markbot.agent.tokens import estimate_messages_tokens as _estimate_messages_tokens

from loguru import logger

_PROTECTED_TOOL_NAMES = frozenset({
    "todo", "autopilot_list", "autopilot_intake",
    "autopilot_pick_next", "autopilot_verify",
})


class CompactAction(str, Enum):
    COLLAPSE = "collapse"
    MICRO_COMPACT = "micro_compact"
    AUTO_COMPACT = "auto_compact"
    HISTORY_SNIP = "history_snip"
    NONE = "none"


@dataclass
class CompactResult:
    """Result of a compaction pass."""
    action: CompactAction
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    summary: str = ""


@dataclass
class CompactionConfig:
    """Tunable parameters for multi-level compaction."""

    collapse_tool_result_chars: int = 4_000
    micro_compact_keep_turns: int = 6
    auto_compact_keep_recent: int = 5
    snip_keep_messages: int = 10

    threshold_ratio: float = 0.85
    max_compact_output_tokens: int = 4_000

    reserved_output_tokens: int = 8_000
    auto_compact_buffer: int = 13_000


class MultiLevelCompactor:
    """4-tier progressive conversation compaction.

    Usage:
        compactor = MultiLevelCompactor(fallback_manager=fallback_manager)
        result = await compactor.maybe_compact(messages, current_tokens, max_tokens)
        if result.action != CompactAction.NONE:
            messages = result.messages  # use compressed messages
    """

    def __init__(
        self,
        fallback_manager=None,
        config: CompactionConfig | None = None,
    ):
        self.fallback_manager = fallback_manager
        self.config = config or CompactionConfig()
        self._compaction_count = 0
        self._total_tokens_saved = 0

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "compaction_count": self._compaction_count,
            "total_tokens_saved": self._total_tokens_saved,
        }

    def reset_stats(self) -> None:
        self._compaction_count = 0
        self._total_tokens_saved = 0

    @staticmethod
    def _get_protected_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
        protected_ids: set[str] = set()
        for m in messages:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        func = tc.get("function", {})
                        if isinstance(func, dict) and func.get("name") in _PROTECTED_TOOL_NAMES:
                            tc_id = tc.get("id")
                            if tc_id:
                                protected_ids.add(str(tc_id))
        return protected_ids

    async def maybe_compact(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int,
        max_tokens: int,
        task_context: str = "",
    ) -> tuple[list[dict[str, Any]], CompactResult]:
        """Run compaction if needed, trying cheapest strategy first.

        Args:
            task_context: Current task state string to preserve through compaction.
                Gathered from todo/autopilot stores before compaction runs.

        Returns:
            (messages, CompactResult) — messages may be modified in-place or replaced.
        """
        cfg = self.config
        effective_max = max_tokens - cfg.reserved_output_tokens - cfg.auto_compact_buffer
        threshold = effective_max * cfg.threshold_ratio

        if current_tokens < threshold:
            return messages, CompactResult(
                action=CompactAction.NONE,
                messages_before=len(messages),
                messages_after=len(messages),
                tokens_before=current_tokens,
                tokens_after=current_tokens,
            )

        logger.info(
            "[Compactor] Tokens {} / {} (threshold {}), starting multi-level compaction",
            current_tokens, effective_max, int(threshold),
        )

        tokens_before = current_tokens
        msgs_before = len(messages)

        messages = self._apply_collapse(messages)
        tokens_after_collapse = _estimate_messages_tokens(messages)
        logger.debug("[Compactor] After collapse: {} tokens (saved {})", tokens_after_collapse, tokens_before - tokens_after_collapse)

        if tokens_after_collapse < threshold:
            self._record_savings(tokens_before, tokens_after_collapse)
            return messages, CompactResult(
                action=CompactAction.COLLAPSE,
                messages_before=msgs_before,
                messages_after=len(messages),
                tokens_before=tokens_before,
                tokens_after=tokens_after_collapse,
            )

        messages = self._apply_micro_compact(messages)
        tokens_after_micro = _estimate_messages_tokens(messages)
        logger.debug("[Compactor] After micro-compact: {} tokens (saved {})", tokens_after_micro, tokens_before - tokens_after_micro)

        if tokens_after_micro < threshold:
            self._record_savings(tokens_before, tokens_after_micro)
            return messages, CompactResult(
                action=CompactAction.MICRO_COMPACT,
                messages_before=msgs_before,
                messages_after=len(messages),
                tokens_before=tokens_before,
                tokens_after=tokens_after_micro,
            )

        messages, summary = await self._apply_auto_compaction(messages, task_context=task_context)
        tokens_after_auto = _estimate_messages_tokens(messages)
        logger.debug("[Compactor] After auto-compaction: {} tokens (saved {})", tokens_after_auto, tokens_before - tokens_after_auto)

        if tokens_after_auto < threshold:
            self._record_savings(tokens_before, tokens_after_auto)
            return messages, CompactResult(
                action=CompactAction.AUTO_COMPACT,
                messages_before=msgs_before,
                messages_after=len(messages),
                tokens_before=tokens_before,
                tokens_after=tokens_after_auto,
                summary=summary,
            )

        messages = self._apply_history_snip(messages, task_context=task_context)
        tokens_after_snip = _estimate_messages_tokens(messages)
        self._record_savings(tokens_before, tokens_after_snip)
        logger.warning(
            "[Compactor] Had to use history snip (last resort): {} -> {} tokens",
            tokens_before, tokens_after_snip,
        )
        return messages, CompactResult(
            action=CompactAction.HISTORY_SNIP,
            messages_before=msgs_before,
            messages_after=len(messages),
            tokens_before=tokens_before,
            tokens_after=tokens_after_snip,
        )

    def _record_savings(self, before: int, after: int) -> None:
        self._compaction_count += 1
        self._total_tokens_saved += max(0, before - after)

    # ------------------------------------------------------------------
    # Level 1: Context Collapse — truncate oversized tool_result blocks
    # ------------------------------------------------------------------

    def _apply_collapse(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        limit = self.config.collapse_tool_result_chars
        collapsed = 0
        for m in messages:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") not in ("tool_result",):
                    continue
                text = str(block.get("content", ""))
                if len(text) <= limit:
                    continue
                kept = text[:limit]
                omitted = len(text) - limit
                block["content"] = (
                    kept + f"\n\n[... {omitted} chars omitted by context collapse ...]"
                )
                collapsed += 1
        if collapsed:
            logger.info("[Compactor] Collapsed {} oversized tool_result blocks", collapsed)
        return messages

    # ------------------------------------------------------------------
    # Level 2: Micro-Compact — strip old tool results, keep recent N turns
    # ------------------------------------------------------------------

    def _apply_micro_compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        keep = self.config.micro_compact_keep_turns
        protected_ids = self._get_protected_tool_call_ids(messages)
        tool_result_indices = [
            i for i, m in enumerate(messages)
            if (
                m.get("role") in ("tool", "user")
                and isinstance(m.get("content"), list)
                and any(b.get("type") == "tool_result" for b in m["content"])
            )
            or (m.get("role") == "tool" and m.get("content"))
        ]
        to_clear = tool_result_indices[:-keep] if len(tool_result_indices) > keep else []
        cleared = 0
        protected_count = 0
        for idx in to_clear:
            msg = messages[idx]
            role = msg.get("role")
            if role == "tool":
                if msg.get("name") in _PROTECTED_TOOL_NAMES or str(msg.get("tool_call_id", "")) in protected_ids:
                    protected_count += 1
                    continue
                msg["content"] = "[tool result removed by micro-compact]"
                cleared += 1
            elif role == "user" and isinstance(msg.get("content"), list):
                new_content = []
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
                        if str(block.get("tool_call_id", "")) in protected_ids:
                            new_content.append(block)
                            protected_count += 1
                            continue
                        new_content.append({
                            "type": "tool_result",
                            "tool_call_id": block.get("tool_call_id", ""),
                            "content": "[tool result removed by micro-compact]",
                        })
                    else:
                        new_content.append(block)
                msg["content"] = new_content
                cleared += 1
        if cleared:
            logger.info("[Compactor] Micro-compacted {} old tool results", cleared)
        if protected_count:
            logger.info("[Compactor] Protected {} task-related tool results from micro-compact", protected_count)
        return messages

    # ------------------------------------------------------------------
    # Level 3: Auto-Compaction — LLM summary replaces old history
    # ------------------------------------------------------------------

    async def _apply_auto_compaction(
        self, messages: list[dict[str, Any]], *, task_context: str = ""
    ) -> tuple[list[dict[str, Any]], str]:
        keep_recent = self.config.auto_compact_keep_recent
        if len(messages) <= keep_recent * 2:
            return messages, ""

        # Split messages at the recent boundary, then adjust backward so that
        # no tool result in the "recent" part is orphaned from its matching
        # assistant tool_calls message.  Without this, compaction creates
        # orphan tool results that corrupt session history on the next turn.
        split = max(0, len(messages) - keep_recent * 2)
        recent_messages = messages[split:]
        # Collect tool_call_ids that are referenced in recent tool messages.
        needed_ids: set[str] = set()
        for m in recent_messages:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                needed_ids.add(str(m["tool_call_id"]))
        if needed_ids:
            # Check which of those ids are declared by assistant messages
            # already inside recent_messages.
            declared_in_recent: set[str] = set()
            for m in recent_messages:
                if m.get("role") == "assistant":
                    for tc in m.get("tool_calls") or []:
                        if isinstance(tc, dict) and tc.get("id"):
                            declared_in_recent.add(str(tc["id"]))
            missing = needed_ids - declared_in_recent
            if missing:
                # Walk backward from the split point to find the assistant
                # messages that declare the missing ids, then continue back
                # to the preceding user message to keep the turn intact.
                earliest: int | None = None
                for i in range(split - 1, max(0, split - 40), -1):
                    m = messages[i]
                    if m.get("role") == "assistant":
                        for tc in m.get("tool_calls") or []:
                            if isinstance(tc, dict) and tc.get("id"):
                                tid = str(tc["id"])
                                if tid in missing:
                                    missing.discard(tid)
                                    if earliest is None or i < earliest:
                                        earliest = i
                    if not missing:
                        break
                if earliest is not None:
                    # Extend split back to the user message that started the
                    # turn containing the earliest matching assistant message.
                    for i in range(earliest, max(0, earliest - 10), -1):
                        if messages[i].get("role") == "user":
                            split = i
                            break
                    else:
                        split = max(0, earliest)
        messages_to_compact = messages[:split]
        recent_messages = messages[split:]

        conversation_text = self._format_messages_for_compact(messages_to_compact)
        if task_context:
            conversation_text += f"\n\n{task_context}"
        try:
            summary = await self._generate_summary(conversation_text)
        except Exception as e:
            logger.error(f"[Compactor] Auto-compaction LLM failed: {e}")
            summary = self._simple_truncation_summary(messages_to_compact)
            if not summary:
                return messages, ""
        formatted = self._format_summary(summary)

        task_section = ""
        if task_context:
            task_section = f"\n\n{task_context}\n\n"

        compact_msg = {
            "role": "system",
            "content": (
                "This session is being continued from a previous conversation "
                "that ran out of context. The summary below covers the earlier portion.\n\n"
                + formatted
                + "\n\nRecent messages are preserved verbatim.\n\n"
                + task_section
                + "Continue the conversation from where it left off without asking further questions. "
                "Resume directly — do not acknowledge the summary, do not recap. "
                "Pick up the last task as if the break never happened."
            ),
        }
        logger.info(
            "[Compactor] Auto-compacted: {} old messages summarized, {} recent kept",
            len(messages_to_compact), len(recent_messages),
        )
        return [compact_msg] + recent_messages, formatted

    # ------------------------------------------------------------------
    # Level 4: History Snip — force drop oldest messages (last resort)
    # ------------------------------------------------------------------

    def _apply_history_snip(self, messages: list[dict[str, Any]], *, task_context: str = "") -> list[dict[str, Any]]:
        keep = self.config.snip_keep_messages
        if len(messages) <= keep:
            return messages
        snipped = messages[-keep:]
        task_section = ""
        if task_context:
            task_section = f"\n\n{task_context}"
        snip_notice = {
            "role": "system",
            "content": (
                f"[{len(messages) - keep} earlier messages were removed by context snip "
                f"to fit within the context window.]{task_section}"
            ),
        }
        result = [snip_notice] + snipped

        declared: set[str] = set()
        cleaned: list[dict[str, Any]] = []
        for msg in result:
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    logger.debug(
                        "[Compactor] History snip dropped orphan tool_result (id={})",
                        tid,
                    )
                    continue
            cleaned.append(msg)

        logger.warning(
            "[Compactor] History snip: {} -> {} messages (dropped {}, cleaned {} orphan tools)",
            len(messages), len(cleaned), len(messages) - keep, len(result) - len(cleaned),
        )
        return cleaned

    # ------------------------------------------------------------------
    # Summary generation (shared with legacy interface)
    # ------------------------------------------------------------------

    COMPACT_PROMPT = """CRITICAL: You are a summarization engine. Your ONLY output is an <analysis> block followed by a <summary> block. You have NO tools. You CANNOT call tools. Do NOT pretend to call tools. Do NOT write "TOOL:" or "<tool_call>" or any tool-like text. If you write tool calls, you have FAILED.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts.

Your summary MUST include these sections:

1. Primary Request and Intent: Capture all of the user's explicit requests in detail
2. Key Technical Concepts: List important concepts, technologies, frameworks discussed
3. Files and Code Sections: Enumerate specific files examined/modified/created
4. Errors and fixes: List errors encountered and how they were fixed
5. Problem Solving: Document problems solved and ongoing troubleshooting
6. All user messages: List ALL user messages verbatim (not tool results)
7. Pending Tasks: List ALL pending and in-progress tasks with full details. If a [CURRENT TASK STATE] section appears in the input, you MUST include ALL items from it verbatim — do not omit, merge, or summarize any task item. Preserve each task's ID, description, status, and priority exactly as given.
8. Current Work: Describe precisely what was being worked on immediately before this summary. Include the specific step, sub-task, or tool call that was in progress, so work can resume seamlessly.
9. Optional Next Step: Next step directly related to the most recent work

FINAL REMINDER: Plain text ONLY. <analysis>...</analysis> then <summary>...</summary>. Nothing else. No tools, no TOOL:, no function calls, no JSON.
"""

    def _format_messages_for_compact(self, messages: list[dict[str, Any]]) -> str:
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if content is None:
                content = ""
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if text is None:
                                text = ""
                            text_parts.append(text)
                        elif block.get("type") == "image":
                            text_parts.append("[image]")
                        elif block.get("type") == "tool_use":
                            text_parts.append(f"[tool_use: {block.get('name', 'unknown')}]")
                        elif block.get("type") == "tool_result":
                            text_parts.append("[tool_result]")
                content = "\n".join(text_parts)
            lines.append(f"{role.upper()}: {content}")
        return "\n\n".join(lines)

    async def _generate_summary(self, conversation_text: str) -> str:
        if self.fallback_manager:
            try:
                from markbot.providers.base import LLMResponse

                response, _ = await self.fallback_manager.chat_with_fallback(
                    messages=[
                        {"role": "system", "content": self.COMPACT_PROMPT},
                        {"role": "user", "content": conversation_text},
                    ],
                )
                if response.finish_reason == "error":
                    error_msg = response.content or "Unknown error"
                    logger.error(f"[Compactor] LLM returned error: {error_msg}")
                    raise RuntimeError(f"Summary generation failed: {error_msg}")
                return response.content or "[Empty summary]"
            except Exception as e:
                logger.error(f"[Compactor] Failed to generate summary with provider: {e}")
                raise

        logger.warning("[Compactor] No LLM available, using simple truncation summary")
        lines = conversation_text.split("\n\n")[:5]
        return f"[Simple summary - first 5 message pairs]\n" + "\n\n".join(lines)

    def _simple_truncation_summary(self, messages: list[dict[str, Any]]) -> str:
        """Generate a simple summary without LLM when auto-compaction LLM fails.

        Extracts key information: user messages, tool calls, and errors.
        Much better than history snip which drops everything.
        """
        user_msgs = []
        tool_calls = []
        errors = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                text = content if isinstance(content, str) else str(content)[:200]
                if text.strip():
                    user_msgs.append(text.strip()[:300])
            elif role == "assistant":
                tcs = msg.get("tool_calls") or []
                for tc in tcs:
                    if isinstance(tc, dict):
                        name = tc.get("function", {}).get("name", "unknown")
                        tool_calls.append(name)
            elif role == "tool":
                if isinstance(content, str) and ("error" in content.lower() or "failed" in content.lower()):
                    errors.append(content[:200])

        parts = ["[Auto-generated summary (LLM unavailable)]"]

        if user_msgs:
            parts.append("\nUser requests:")
            for i, m in enumerate(user_msgs[-10:], 1):
                parts.append(f"  {i}. {m}")

        if tool_calls:
            parts.append(f"\nTools used: {', '.join(tool_calls[-30:])}")

        if errors:
            parts.append("\nErrors encountered:")
            for e in errors[-5:]:
                parts.append(f"  - {e}")

        return "\n".join(parts)

    @staticmethod
    def _format_summary(summary: str) -> str:
        # Strip hallucinated tool-call patterns that weak models sometimes emit
        summary = re.sub(
            r'(?:TOOL:\s*)?<tool_calls?>[\s\S]*?</tool_calls?>', '', summary,
        )
        summary = re.sub(
            r'(?:TOOL:\s*)?\{[\s\S]*?"(?:name|function)"[\s\S]*?\}', '', summary,
        )
        summary = re.sub(
            r'^TOOL:\s*#\s*Search\s*Results.*?(?=\n\n|\n\*|\Z)', '',
            summary, flags=re.MULTILINE | re.IGNORECASE,
        )
        # Strip <function_call> patterns
        summary = re.sub(
            r'<function_calls?>[\s\S]*?</function_calls?>', '', summary,
        )
        # Strip JSON tool_call blocks
        summary = re.sub(
            r'\{\s*"tool_calls?"\s*:[\s\S]*?\}', '', summary,
        )
        # Strip stray TOOL: lines
        summary = re.sub(r'^TOOL:.*$', '', summary, flags=re.MULTILINE)

        formatted = re.sub(r'<analysis>[\s\S]*?</analysis>', '', summary)
        summary_match = re.search(r'<summary>([\s\S]*?)</summary>', formatted)
        if summary_match:
            content = summary_match.group(1) or ''
            formatted = re.sub(
                r'<summary>[\s\S]*?</summary>',
                f'Summary:\n{content.strip()}',
                formatted,
            )
        else:
            # If no <summary> block found (weak model), treat everything after
            # <analysis> (or the whole text) as the summary
            formatted = re.sub(r'<analysis>[\s\S]*?</analysis>', '', summary)

        formatted = re.sub(r'\n{3,}', '\n\n', formatted).strip()
        return formatted






