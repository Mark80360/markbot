"""Memory compaction hook for long-term memory archival.

Responsible for archiving older conversation messages into long-term
memory (compressed_summary + async summary tasks) when the context
window approaches its limit.

NOTE: This hook handles **long-term memory archival** only. Immediate
context window pressure (truncating tool results, dropping old messages)
is handled by MultiLevelCompactor in the agent loop. The two systems
coordinate via the ``skip_context_compact`` flag: when
MultiLevelCompactor has already performed aggressive compaction
(AUTO_COMPACT / HISTORY_SNIP), this hook skips its own context
compaction step and only triggers async summary archival.

Already-compacted messages are marked with ``_markbot_compacted``
in their metadata and skipped during subsequent compaction passes
to avoid double-compressing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from markbot.bus.events import make_session_key

from markbot.agent.tokens import estimate_tokens as _estimate_tokens

if TYPE_CHECKING:
    from markbot.memory.base import BaseMemoryManager

_MEMORY_COMPACT_KEEP_RECENT = 4
_COMPACTED_MARKER = "_markbot_compacted"


def _message_content_hash(m: dict) -> str:
    """Stable short hash of a message's textual content.

    Used as a content-addressed high-water mark for archived messages so
    the skip logic is robust to insertion/deletion between turns (unlike
    a positional count, which drifts when MultiLevelCompactor snips
    messages or the history is reloaded with different framing).
    """
    import hashlib
    role = m.get("role", "")
    content = m.get("content", "")
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    parts.append(f"[tool:{b.get('name', '')}]")
        content = chr(10).join(parts)
    elif not isinstance(content, str):
        content = str(content or "")
    return hashlib.sha256(f"{role}:{content}".encode("utf-8")).hexdigest()[:32]


def _skip_archived_by_tail(
    messages: list[dict],
    archived_tail: str,
) -> tuple[list[dict], int, bool]:
    """Skip already-archived prefix if the tail hash is found.

    If the tail hash cannot be found, return the original messages.  That
    conservatively re-compacts some history, but avoids the dangerous failure
    mode of dropping all candidates because the old tail was snipped or the
    history was reloaded with different framing.
    """
    if not archived_tail:
        return messages, 0, False

    for idx, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") == "system":
            continue
        if _message_content_hash(m) == archived_tail:
            return messages[idx + 1 :], idx + 1, True
    return messages, 0, False

# Exception types that indicate a programming bug (wrong type, missing
# attribute, undefined name) rather than a transient operational failure.
# These propagate to the caller so the error is visible instead of being
# silently swallowed by the broad ``except Exception`` below.
_PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError, ImportError)


class MemoryCompactionHook:
    """Hook for automatic memory archival when context is full.

    **Scope**: Long-term memory archival and session-level compression.
    This hook persists conversation knowledge into MEMORY.md (via async
    summary tasks) and compresses older messages into ``compressed_summary``
    (via ``memory_manager.compact_memory()``).  It does NOT truncate
    tool results or drop messages — that is MultiLevelCompactor's job.

    **Coordination with MultiLevelCompactor**:
    Both systems may run in the same iteration.  When MultiLevelCompactor
    has already performed aggressive compaction (AUTO_COMPACT / HISTORY_SNIP),
    the iteration runner passes ``skip_context_compact=True`` so this hook
    only runs Phase 1 (async summary archival) and skips Phase 2 (context
    compaction), avoiding redundant LLM summarization.

    **Intentional dual summarization**:
    Phase 1 (async summary archival) and MultiLevelCompactor's auto-compact
    serve different purposes and write to different stores:
    - MultiLevelCompactor → replaces in-flight messages with a system-prompt
      summary (immediate context relief, not persisted to MEMORY.md)
    - Phase 1 → writes to MEMORY.md via add_async_summary_task (long-term
      memory archival, survives across sessions)
    Both may process the same messages, but their outputs are independent.

    Two-phase operation:
    1. **Async summary archival** (always runs when messages need archiving):
       Schedules a background summary task via ``add_async_summary_task()``
       to persist conversation knowledge into MEMORY.md.
    2. **Context compaction** (skipped when MultiLevelCompactor already handled it):
       Compresses older messages into ``compressed_summary`` to free up
       context window space.  This is the "session-level" compression that
       keeps the current conversation flowing.

    The ``skip_context_compact`` parameter allows the agent loop to
    signal that MultiLevelCompactor already performed aggressive
    compaction, so only the archival phase should run.
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
        *,
        skip_context_compact: bool = False,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> str | None:
        """Pre-reasoning hook to check and compact memory if needed.

        Args:
            messages: List of conversation message dicts
            system_prompt: Current system prompt string
            skip_context_compact: If True, skip context compaction phase
                (MultiLevelCompactor already handled it). Async summary
                archival still runs if applicable.
            channel: Message channel for session-scoped compaction.
            chat_id: Chat ID for session-scoped compaction.

        Returns:
            New compressed summary if context compaction occurred, None otherwise
        """
        try:
            if not getattr(self.memory_manager, "_started", False) and not getattr(self.memory_manager, "_memory_store", None):
                return None

            session_key = make_session_key(channel, chat_id)

            str_token_count = _estimate_tokens(
                system_prompt
            ) + _estimate_tokens(
                self.memory_manager.get_compressed_summary(session_key=session_key)
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

            messages_to_compact = [
                m.to_dict() if hasattr(m, 'to_dict') else m
                for m in messages_to_compact
                if (hasattr(m, 'to_dict') and hasattr(m, 'role')) or
                   (isinstance(m, dict) and "role" in m)
            ]

            if not messages_to_compact:
                return None

            messages_to_compact = self._filter_already_compacted(messages_to_compact)
            if not messages_to_compact:
                logger.info("All messages already compacted, skipping")
                return None

            # Skip messages that were archived in previous turns.
            # The _markbot_compacted metadata marker does not survive
            # session save/load (get_history strips metadata), so we use
            # a content-addressed high-water mark: the hash of the last
            # message archived in the previous compaction.  This is
            # robust to message insertion/deletion between turns, unlike
            # a positional count which drifts when messages are snipped.
            archived_tail = self.memory_manager.get_archived_tail_hash(session_key=session_key)
            if archived_tail:
                filtered, skipped, found_tail = _skip_archived_by_tail(
                    messages_to_compact, archived_tail,
                )
                if not found_tail:
                    logger.info(
                        "Archived tail {} not found; re-compacting from available history",
                        archived_tail[:8],
                    )
                messages_to_compact = filtered
                if not filtered:
                    logger.info(
                        "All compactable messages already archived (tail={})",
                        archived_tail[:8],
                    )
                    return None
                if skipped > 0:
                    logger.info("Skipped {} already-archived messages (tail match={})", skipped, found_tail)

            if not is_valid:
                logger.warning("Invalid messages during compaction, adjusting...")
                keep_length = _MEMORY_COMPACT_KEEP_RECENT
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

            messages_to_compact = self._filter_already_compacted(messages_to_compact)
            if not messages_to_compact:
                logger.info("All messages already compacted after adjustment, skipping")
                return None

            # Re-apply tail-hash skip after is_valid adjustment
            # (the adjustment may have reset messages_to_compact).
            if archived_tail:
                filtered, skipped, found_tail = _skip_archived_by_tail(
                    messages_to_compact, archived_tail,
                )
                if not found_tail:
                    logger.info(
                        "Archived tail {} not found after adjustment; re-compacting from available history",
                        archived_tail[:8],
                    )
                messages_to_compact = filtered
                if not filtered:
                    logger.info(
                        "All compactable messages already archived after adjustment"
                    )
                    return None

            # Phase 1: Context compaction (skip if MultiLevelCompactor
            # already handled it).  We run this BEFORE the async summary
            # task so the compact summary can be reused as input — this
            # avoids a redundant LLM call with overlapping content.
            pre_compact_tokens = _estimate_tokens(
                "".join(
                    m.get("content", "") if isinstance(m.get("content", ""), str)
                    else str(m.get("content") or "")
                    for m in messages_to_compact
                )
            )
            pre_total_tokens = _estimate_tokens(
                system_prompt
                + self.memory_manager.get_compressed_summary(session_key=session_key)
                + "".join(
                    m.get("content", "") if isinstance(m.get("content", ""), str)
                    else str(m.get("content") or "")
                    for m in messages
                )
            )

            compact_content: str | None = None

            if skip_context_compact:
                logger.info(
                    "[Compaction] Skipping context compaction "
                    "(MultiLevelCompactor already applied); "
                    "async summary archival still triggered for {} messages",
                    len(messages_to_compact),
                )
            else:
                logger.info(
                    "[Compaction] Starting — compacting {} messages "
                    "({} tokens), total context ~{} tokens",
                    len(messages_to_compact),
                    pre_compact_tokens,
                    pre_total_tokens,
                )

                if self.context_compact_enabled:
                    compressed_summary = self.memory_manager.get_compressed_summary(session_key=session_key)
                    compact_content = await self.memory_manager.compact_memory(
                        messages=messages_to_compact,
                        previous_summary=compressed_summary,
                    )
                    if not compact_content:
                        logger.warning("Context compaction failed.")
                    else:
                        post_summary_tokens = _estimate_tokens(compact_content)
                        saved_tokens = pre_compact_tokens - post_summary_tokens
                        logger.info(
                            "[Compaction] Completed — summary: {} tokens, "
                            "saved ~{} tokens ({:.0f}% reduction)",
                            post_summary_tokens,
                            max(saved_tokens, 0),
                            (saved_tokens / pre_compact_tokens * 100)
                            if pre_compact_tokens > 0
                            else 0,
                        )
                        self._mark_compacted(messages_to_compact)
                        self.memory_manager.set_compressed_summary(
                            compact_content, session_key=session_key,
                        )
                        # Persist a content-addressed high-water mark so
                        # the next turn can skip these messages by content
                        # match rather than by fragile positional count.
                        last_msg = messages_to_compact[-1] if messages_to_compact else None
                        if isinstance(last_msg, dict):
                            self.memory_manager.set_archived_tail_hash(
                                _message_content_hash(last_msg),
                                session_key=session_key,
                            )
                else:
                    logger.info("Context compaction skipped")

            # Phase 2: Trigger async summary archival for long-term memory.
            # Pass the compact summary as input so summary_memory can reuse
            # it instead of making a separate LLM call over raw messages.
            if self.memory_summary_enabled:
                self.memory_manager.add_async_summary_task(
                    messages=messages_to_compact,
                    compact_summary=compact_content or "",
                )

            return compact_content

        except _PROGRAMMING_ERRORS:
            # Programming bugs (TypeError, AttributeError, …) must not be
            # swallowed — they indicate a real defect that should surface
            # to the caller instead of degrading silently.
            raise
        except Exception as e:
            # Operational failures (LLM timeout, provider error, corrupt
            # response, …) are recoverable: log and degrade gracefully so
            # the agent can continue without compaction this turn.
            logger.exception("Failed to compact memory in pre_reasoning hook: {}", e)
            return None

    @staticmethod
    def _filter_already_compacted(messages: list[dict]) -> list[dict]:
        """Filter out messages that have already been compacted."""
        return [
            m for m in messages
            if not m.get("metadata", {}).get(_COMPACTED_MARKER, False)
            if isinstance(m, dict)
        ]

    @staticmethod
    def _mark_compacted(messages: list[dict]) -> None:
        """Mark messages as compacted in-place via metadata."""
        for m in messages:
            if isinstance(m, dict):
                metadata = m.setdefault("metadata", {})
                if isinstance(metadata, dict):
                    metadata[_COMPACTED_MARKER] = True

    @staticmethod
    def _check_valid_messages(messages: list[dict]) -> bool:
        return all(
            isinstance(m, dict) and "role" in m for m in messages
        )
