"""MemoryProvider ABC — pluggable memory provider interface.

Defines a standard lifecycle for memory backends so that
different providers are interchangeable.

Lifecycle (called by MemoryManager):
  initialize()         -> connect, create resources, warm up
  system_prompt_block() -> static text for the system prompt
  prefetch(query)       -> background recall before each turn
  sync_turn(user, asst) -> persist after each turn
  shutdown()            -> clean exit

Optional hooks:
  on_session_switch()   -> mid-process session_id rotation
  on_pre_compress()     -> extract before context compression
  on_memory_write()     -> mirror built-in memory writes
  on_delegation()       -> parent-side observation of subagent work
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from loguru import logger


class MemoryProvider(ABC):
    """Abstract base class for pluggable memory providers.

    Implement this interface to create a custom memory backend for MarkBot.
    Only one external provider is active at a time.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'markbot', 'chroma', 'custom')."""

    # -- Core lifecycle (implement these) ------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider is configured and ready.

        Called during agent init to decide whether to activate the provider.
        Should not make network calls 鈥?just check config and installed deps.
        """

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session.

        Called once at agent startup. May create resources (banks, tables),
        establish connections, start background threads, etc.

        Kwargs may include:
          - working_dir (str): Workspace directory for file-based storage
          - agent_id (str): Agent identifier for per-agent scoping
          - language (str): Language preference
          - timezone (str): Timezone setting
        """

    def system_prompt_block(self) -> str:
        """Return text to include in the system prompt.

        Called during system prompt assembly. Return empty string to skip.
        This is for STATIC provider info (instructions, status). Prefetched
        recall context is injected separately via prefetch().
        """
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn.

        Called before each API call. Return formatted text to inject as
        context, or empty string if nothing relevant. Implementations
        should be fast 鈥?use background threads for the actual recall
        and return cached results here.

        Args:
            query: The user message text to use as the search query.
            session_id: Optional session identifier for scoped recall.

        Returns:
            Formatted context string, or empty string if nothing relevant.
        """
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn.

        Called after each turn completes. The result will be consumed
        by prefetch() on the next turn. Default is no-op 鈥?providers
        that do background prefetching should override this.

        Args:
            query: The user message text to use as the search query.
            session_id: Optional session identifier for scoped recall.
        """

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """Persist a completed turn to the backend.

        Called after each turn. Use to store conversation history,
        update embeddings, or trigger async processing.

        Args:
            user_content: The user's message content.
            assistant_content: The assistant's response content.
            session_id: Optional session identifier for scoped storage.
        """

    def shutdown(self) -> None:
        """Clean up resources. Called during agent shutdown."""

    # -- Optional hooks ------------------------------------------------------

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        """Called when the agent switches session_id mid-process.

        Providers that cache per-session state should update or reset
        that state here so subsequent writes land in the correct session.

        Args:
            new_session_id: The session_id the agent just switched to.
            parent_session_id: The previous session_id, if meaningful.
            reset: True when this is a genuinely new conversation, not a
                resumption of an existing one.
        """

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Called before context compression discards old messages.

        Use to extract insights from messages about to be compressed.

        Args:
            messages: The list of messages that will be summarized/discarded.

        Returns:
            Text to include in the compression summary prompt, or empty string.
        """
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Called when the built-in memory tool writes an entry.

        Use to mirror built-in memory writes to your backend.

        Args:
            action: 'add', 'replace', or 'remove'.
            target: 'memory' or 'user'.
            content: The entry content.
            metadata: Structured provenance for the write.
        """

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs,
    ) -> None:
        """Called on the PARENT agent when a subagent completes.

        The parent's memory provider gets the task+result pair as an
        observation of what was delegated and what came back.

        Args:
            task: The delegation prompt.
            result: The subagent's final response.
            child_session_id: The subagent's session_id.
        """


__all__ = ["MemoryProvider"]
