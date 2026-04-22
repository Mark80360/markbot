"""Memory compaction hook for managing context window.

Monitors token usage and automatically compacts older messages when
the context window approaches its limit.

Ported from CoPaw's MemoryCompactionHook — runs as pre_reasoning hook.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from markbot.agent.tokens import estimate_tokens as _estimate_tokens

if TYPE_CHECKING:
    from ..base import BaseMemoryManager

MEMORY_COMPACT_KEEP_RECENT = 4


class MemoryCompactionHook:
    """Hook for automatic memory compaction when context is full.

    Monitors token count of messages and triggers compaction when it
    exceeds threshold. Preserves system prompt and recent messages while
    summarizing older conversation history.
    """

    def __init__(
        self,
        memory_manager: "BaseMemoryManager",
        memory_compact_threshold: int = 50_000,
        memory_compact_reserve: int = 10_000,
        context_compact_enabled: bool = True,
        memory_summary_enabled: bool = True,
    ):
        self.memory_manager = memory_manager
        self.memory_compact_threshold = memory_compact_threshold
        self.memory_compact_reserve = memory_compact_reserve
        self.context_compact_enabled = context_compact_enabled
        self.memory_summary_enabled = memory_summary_enabled

    async def __call__(
        self,
        messages: list[dict],
        system_prompt: str = "",
    ) -> str | None:
        """Pre-reasoning hook to check and compact memory if needed.

        Args:
            messages: List of conversation message dicts
            system_prompt: Current system prompt string

        Returns:
            New compressed summary if compaction occurred, None otherwise
        """
        try:
            if not self.memory_manager._reme:
                return None

            str_token_count = _estimate_tokens(
                system_prompt + getattr(self.memory_manager, '_compressed_summary', '')
            )

            left_compact_threshold = (
                self.memory_compact_threshold - str_token_count
            )

            if left_compact_threshold <= 0:
                logger.warning(
                    "memory_compact_threshold is set too low; "
                    "combined token length exceeds configured threshold."
                )
                return None

            result = await self.memory_manager.check_context(
                messages=messages,
                memory_compact_threshold=left_compact_threshold,
                memory_compact_reserve=self.memory_compact_reserve,
            )

            # Handle error case: result might be a string if an exception occurred
            if isinstance(result, str):
                logger.warning(
                    "check_context returned error message: %s",
                    result,
                )
                return None

            if result is None or len(result) < 3:
                return None

            messages_to_compact, _, is_valid = result[0], result[1], result[2]

            if not messages_to_compact:
                return None

            if not isinstance(messages_to_compact, list):
                logger.warning(
                    "check_context returned unexpected type for messages_to_compact: %s",
                    type(messages_to_compact).__name__,
                )
                return None

            # Convert Msg objects to dicts if necessary
            messages_to_compact = [
                m.to_dict() if hasattr(m, 'to_dict') else m
                for m in messages_to_compact
                if (hasattr(m, 'to_dict') and hasattr(m, 'role')) or
                   (isinstance(m, dict) and "role" in m)
            ]

            if not messages_to_compact:
                return None

            if not is_valid:
                logger.warning("Invalid messages during compaction, adjusting...")
                keep_length = MEMORY_COMPACT_KEEP_RECENT
                messages_length = len(messages)
                while keep_length > 0 and not self._check_valid_messages(
                    messages[max(messages_length - keep_length, 0):]
                ):
                    keep_length -= 1

                if keep_length > 0:
                    messages_to_compact = messages[:max(messages_length - keep_length, 0)]
                else:
                    messages_to_compact = messages

            if not messages_to_compact:
                return None

            if self.memory_summary_enabled:
                self.memory_manager.add_async_summary_task(
                    messages=messages_to_compact,
                )

            logger.info("Context compaction started...")

            if self.context_compact_enabled:
                compressed_summary = getattr(self.memory_manager, '_compressed_summary', '')
                compact_content = await self.memory_manager.compact_memory(
                    messages=messages_to_compact,
                    previous_summary=compressed_summary,
                )
                if not compact_content:
                    logger.warning("Context compaction failed.")
                else:
                    logger.info("Context compaction completed")
                    if hasattr(self.memory_manager, 'set_compressed_summary'):
                        self.memory_manager.set_compressed_summary(compact_content)
                    else:
                        self.memory_manager._compressed_summary = compact_content
                    return compact_content
            else:
                logger.info("Context compaction skipped")

            return None

        except Exception as e:
            logger.exception(f"Failed to compact memory in pre_reasoning hook: {e}")
            return None

    @staticmethod
    def _check_valid_messages(messages: list[dict]) -> bool:
        return all(
            isinstance(m, dict) and "role" in m for m in messages
        )
