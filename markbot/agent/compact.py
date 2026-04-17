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

    async def maybe_compact(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int,
        max_tokens: int,
    ) -> tuple[list[dict[str, Any]], CompactResult]:
        """Run compaction if needed, trying cheapest strategy first.

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

        messages, summary = await self._apply_auto_compaction(messages)
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

        messages = self._apply_history_snip(messages)
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
        tool_result_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") in ("tool", "user")
            and isinstance(m.get("content"), list)
            and any(b.get("type") == "tool_result" for b in m["content"])
            or (m.get("role") == "tool" and m.get("content"))
        ]
        to_clear = tool_result_indices[:-keep] if len(tool_result_indices) > keep else []
        cleared = 0
        for idx in to_clear:
            msg = messages[idx]
            role = msg.get("role")
            if role == "tool":
                msg["content"] = "[tool result removed by micro-compact]"
                cleared += 1
            elif role == "user" and isinstance(msg.get("content"), list):
                new_content = []
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
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
        return messages

    # ------------------------------------------------------------------
    # Level 3: Auto-Compaction — LLM summary replaces old history
    # ------------------------------------------------------------------

    async def _apply_auto_compaction(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str]:
        keep_recent = self.config.auto_compact_keep_recent
        if len(messages) <= keep_recent * 2:
            return messages, ""

        messages_to_compact = messages[:-keep_recent * 2]
        recent_messages = messages[-keep_recent * 2:]

        conversation_text = self._format_messages_for_compact(messages_to_compact)
        try:
            summary = await self._generate_summary(conversation_text)
        except Exception as e:
            logger.error(f"[Compactor] Auto-compaction failed, falling back to history snip: {e}")
            # Return original messages to trigger fallback to history snip
            return messages, ""
        formatted = self._format_summary(summary)

        compact_msg = {
            "role": "system",
            "content": (
                "This session is being continued from a previous conversation "
                "that ran out of context. The summary below covers the earlier portion.\n\n"
                + formatted
                + "\n\nRecent messages are preserved verbatim.\n\n"
                "Continue the conversation from where it left off without asking further questions. "
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

    def _apply_history_snip(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        keep = self.config.snip_keep_messages
        if len(messages) <= keep:
            return messages
        snipped = messages[-keep:]
        snip_notice = {
            "role": "system",
            "content": (
                f"[{len(messages) - keep} earlier messages were removed by context snip "
                f"to fit within the context window.]"
            ),
        }
        logger.warning(
            "[Compactor] History snip: {} -> {} messages (dropped {})",
            len(messages), keep, len(messages) - keep,
        )
        return [snip_notice] + snipped

    # ------------------------------------------------------------------
    # Summary generation (shared with legacy interface)
    # ------------------------------------------------------------------

    COMPACT_PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts.

Your summary should include these sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List important concepts, technologies, frameworks discussed
3. Files and Code Sections: Enumerate specific files examined/modified/created with code snippets
4. Errors and fixes: List errors encountered and how they were fixed
5. Problem Solving: Document problems solved and ongoing troubleshooting
6. All user messages: List ALL user messages that are not tool results
7. Pending Tasks: Outline pending tasks explicitly requested
8. Current Work: Describe precisely what was being worked on immediately before this summary
9. Optional Next Step: Next step directly related to the most recent work

REMINDER: Do NOT call any tools. Respond with plain text only — an <analysis> block followed by a <summary> block.
"""

    def _format_messages_for_compact(self, messages: list[dict[str, Any]]) -> str:
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
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
                    max_tokens=self.config.max_compact_output_tokens,
                    temperature=0.3,
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

    @staticmethod
    def _format_summary(summary: str) -> str:
        formatted = re.sub(r'<analysis>[\s\S]*?</analysis>', '', summary)
        summary_match = re.search(r'<summary>([\s\S]*?)</summary>', formatted)
        if summary_match:
            content = summary_match.group(1) or ''
            formatted = re.sub(r'<summary>[\s\S]*?</summary>', f'Summary:\n{content.strip()}', formatted)
        return re.sub(r'\n\n+', '\n\n', formatted).strip()






