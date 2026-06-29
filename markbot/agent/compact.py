"""Multi-level conversation context compression service.

4-tier progressive compression strategy (inspired by Claude's context management):
  Level 1: Context Collapse  — Truncate individual tool_result blocks that exceed char limit (head+tail)
  Level 2: Micro-Compact      — Remove old tool_result content, keep recent N turns
  Level 3: Auto-Compaction    — LLM generates summary to replace old history
  Level 4: History Snip       — Force-drop oldest messages (last resort)

Each level is progressively more aggressive and expensive.
The system tries the cheapest level first and escalates only when needed.

Enhanced features (inspired by OpenHarness):
- Head+tail context collapse preserving both ends of content
- CompactAttachment system for preserving key context across compaction
- Tool output offloading for oversized tool results
- PTL (Prompt-Too-Long) retry with head truncation
- Consecutive compaction failure protection
- Compaction progress callback for UI notification
- Image placeholder replacement during compaction summarization
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from loguru import logger

from markbot.agent.tokens import estimate_messages_tokens as _estimate_messages_tokens

_PROTECTED_TOOL_NAMES = frozenset({
    "todo", "autopilot_list", "autopilot_intake",
    "autopilot_pick_next", "autopilot_verify",
})

CONTEXT_COLLAPSE_HEAD_CHARS = 900
CONTEXT_COLLAPSE_TAIL_CHARS = 500
MAX_COMPACT_ATTACHMENTS = 6
MAX_COMPACT_ATTACHMENT_CHARS = 4_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
DEFAULT_TOOL_OUTPUT_INLINE_CHARS = 16_000
DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS = 3_000
PTL_RETRY_MARKER = "[earlier conversation truncated for compaction retry]"

_PTL_ERROR_PATTERNS = (
    "prompt too long",
    "context length",
    "maximum context",
    "context window",
    "too many tokens",
    "too large for the model",
    "maximum context length",
    "exceed_context",
    "exceeds the available context size",
    "available context size",
)


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
    attachments: list[CompactAttachment] = field(default_factory=list)


@dataclass
class CompactionConfig:
    """Tunable parameters for multi-level compaction."""

    collapse_tool_result_chars: int = 4_000
    collapse_head_chars: int = CONTEXT_COLLAPSE_HEAD_CHARS
    collapse_tail_chars: int = CONTEXT_COLLAPSE_TAIL_CHARS
    micro_compact_keep_turns: int = 6
    auto_compact_keep_recent: int = 5
    snip_keep_messages: int = 10

    threshold_ratio: float = 0.85
    max_compact_output_tokens: int = 4_000

    reserved_output_tokens: int = 8_000
    auto_compact_buffer: int = 13_000

    tool_output_inline_chars: int = DEFAULT_TOOL_OUTPUT_INLINE_CHARS
    tool_output_preview_chars: int = DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS


CompactProgressCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]


@dataclass
class CompactAttachment:
    """Structured compact asset carried across a compaction boundary.

    Preserves key context (file paths, tool names, user goals, artifacts)
    that would otherwise be lost when messages are compressed.
    """

    kind: str
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        header = f"[Compact attachment: {self.kind}] {self.title}".strip()
        text = f"{header}\n{self.body}".strip()
        return {"role": "user", "content": [{"type": "text", "text": text}]}


def is_prompt_too_long_error(exc: Exception) -> bool:
    """Check if an exception indicates the prompt exceeded the context window."""
    text = str(exc).lower()
    return any(needle in text for needle in _PTL_ERROR_PATTERNS)


def collapse_text_head_tail(
    text: str,
    limit: int,
    head_chars: int = CONTEXT_COLLAPSE_HEAD_CHARS,
    tail_chars: int = CONTEXT_COLLAPSE_TAIL_CHARS,
) -> str:
    """Collapse text preserving both head and tail portions.

    For code snippets and tool output, the beginning (imports, setup)
    and end (results, errors) are typically most valuable.
    """
    if len(text) <= limit:
        return text
    # If the configured head+tail exceeds the limit, collapsing would
    # produce output *longer* than just truncating to `limit`.  Shrink
    # head/tail proportionally so the collapsed result still fits.
    if head_chars + tail_chars >= limit:
        # Reserve a small slot for the collapse marker itself.
        marker_overhead = 40
        budget = max(1, limit - marker_overhead)
        head_chars = max(1, budget // 2)
        tail_chars = max(1, budget - head_chars)
    omitted = len(text) - head_chars - tail_chars
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip()
    return f"{head}\n...[collapsed {omitted} chars]...\n{tail}"


def offload_tool_output(
    output: str,
    tool_name: str,
    tool_use_id: str,
    inline_limit: int = DEFAULT_TOOL_OUTPUT_INLINE_CHARS,
    preview_chars: int = DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS,
    artifact_dir: Path | None = None,
) -> tuple[str, Path | None]:
    """Offload oversized tool output to a file, returning inline preview.

    When tool output exceeds inline_limit, saves the full output to a file
    and returns a truncated preview with a reference to the saved file.

    Returns:
        (inline_content, artifact_path) — artifact_path is None if no offload needed.
    """
    if len(output) <= inline_limit:
        return output, None

    if artifact_dir is None:
        from markbot.config.paths import get_artifacts_dir
        artifact_dir = Path(os.environ.get("MARKBOT_ARTIFACT_DIR", str(get_artifacts_dir())))
    artifact_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool_name.strip())[:80] or "tool"
    artifact_path = artifact_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe_name}-{uuid4().hex[:12]}.txt"
    artifact_path.write_text(output, encoding="utf-8", errors="replace")

    preview = output[:preview_chars]
    omitted = max(0, len(output) - len(preview))
    inline = (
        "[Tool output truncated]\n"
        f"Tool: {tool_name}\n"
        f"Tool use id: {tool_use_id}\n"
        f"Original size: {len(output)} chars\n"
        f"Full output saved to: {artifact_path}\n"
        f"Inline preview: first {len(preview)} chars"
    )
    if omitted:
        inline += f" ({omitted} chars omitted)"
    if preview:
        inline += f"\n\nPreview:\n{preview}"
    return inline, artifact_path


def extract_attachments_from_messages(
    messages: list[dict[str, Any]],
) -> list[CompactAttachment]:
    """Extract structured attachments from messages before compaction.

    Captures:
    - File paths referenced in tool calls and results
    - Tool names discovered in the conversation
    - Image references with source paths
    """
    attachments: list[CompactAttachment] = []
    file_paths: list[str] = []
    discovered_tools: list[str] = []
    seen_paths: set[str] = set()
    seen_tools: set[str] = set()
    path_pattern = re.compile(r"(?:path|file):\s*([^\s\)\"]+)")
    image_sources: list[str] = []

    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "image":
                    source = block.get("source_path", "")
                    if source and source not in seen_paths:
                        seen_paths.add(source)
                        image_sources.append(source)
                elif block.get("type") == "text":
                    text = block.get("text", "")
                    for match in path_pattern.findall(text):
                        p = match.strip()
                        if p and p not in seen_paths:
                            seen_paths.add(p)
                            file_paths.append(p)
                elif block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name and name not in seen_tools:
                        seen_tools.add(name)
                        discovered_tools.append(name)
                    inp = block.get("input", {})
                    if isinstance(inp, dict):
                        for key in ("path", "file_path", "filename"):
                            val = str(inp.get(key, "")).strip()
                            if val and val not in seen_paths:
                                seen_paths.add(val)
                                file_paths.append(val)
        elif isinstance(content, str):
            for match in path_pattern.findall(content):
                p = match.strip()
                if p and p not in seen_paths:
                    seen_paths.add(p)
                    file_paths.append(p)

        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    name = tc.get("function", {}).get("name", "")
                    if name and name not in seen_tools:
                        seen_tools.add(name)
                        discovered_tools.append(name)

    if file_paths and len(attachments) < MAX_COMPACT_ATTACHMENTS:
        lines = ["Referenced file paths (may still be relevant):"]
        for p in file_paths[:8]:
            lines.append(f"  - {p}")
        body = "\n".join(lines)
        if len(body) > MAX_COMPACT_ATTACHMENT_CHARS:
            body = body[:MAX_COMPACT_ATTACHMENT_CHARS] + "\n  ..."
        attachments.append(CompactAttachment(
            kind="referenced_files",
            title="Referenced file paths",
            body=body,
            metadata={"paths": file_paths[:8]},
        ))

    if image_sources and len(attachments) < MAX_COMPACT_ATTACHMENTS:
        lines = ["Image sources referenced (payloads omitted from compaction):"]
        for s in image_sources[:4]:
            lines.append(f"  - {s}")
        attachments.append(CompactAttachment(
            kind="image_sources",
            title="Image source references",
            body="\n".join(lines),
            metadata={"sources": image_sources[:4]},
        ))

    if discovered_tools and len(attachments) < MAX_COMPACT_ATTACHMENTS:
        tool_list = ", ".join(discovered_tools[:12])
        attachments.append(CompactAttachment(
            kind="discovered_tools",
            title="Tools used in conversation",
            body=f"Tools invoked: {tool_list}",
            metadata={"tools": discovered_tools[:12]},
        ))

    return attachments


def replace_images_with_placeholders(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace image blocks with text placeholders for compaction summarization.

    Images consume large numbers of tokens but provide little value
    when sent to the summarization LLM. Replace them with compact
    text references.
    """
    changed = False
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue
        new_blocks: list[dict[str, Any]] = []
        msg_changed = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image":
                msg_changed = True
                source = block.get("source_path", "").strip() or "inline"
                new_blocks.append({
                    "type": "text",
                    "text": f"[Image omitted from compaction summarization; source: {source}.]\n",
                })
            else:
                new_blocks.append(block)
        if msg_changed:
            changed = True
            result.append({**msg, "content": new_blocks})
        else:
            result.append(msg)
    return result if changed else messages


def truncate_head_for_ptl_retry(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Drop the oldest prompt rounds when the compact request itself is too large.

    Groups messages by user-prompt rounds and drops the oldest 1/5
    to make the prompt fit within the context window.
    """
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        starts_new_round = (
            message.get("role") == "user"
            and not (
                isinstance(message.get("content"), list)
                and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in message["content"]
                )
            )
            and isinstance(message.get("content"), str)
            and message["content"].strip()
        )
        if starts_new_round and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)

    if len(groups) < 2:
        return None

    drop_count = max(1, len(groups) // 5)
    drop_count = min(drop_count, len(groups) - 1)
    retained = [message for group in groups[drop_count:] for message in group]
    if not retained:
        return None
    if retained[0].get("role") == "assistant":
        return [{"role": "user", "content": PTL_RETRY_MARKER}, *retained]
    return retained


class MultiLevelCompactor:
    """4-tier progressive conversation compaction.

    **Scope**: Immediate context-window pressure relief.
    This compactor operates on the *in-flight message list* to keep
    the current conversation within the model's token limit.  It does
    NOT touch long-term memory (MEMORY.md, compressed_summary).

    **Coordination with MemoryCompactionHook**:
    After this compactor applies AUTO_COMPACT or HISTORY_SNIP, the
    iteration runner sets ``skip_context_compact=True`` when calling
    MemoryCompactionHook, so that hook only triggers async summary
    archival (Phase 1) and skips its own context compaction (Phase 2),
    avoiding redundant LLM summarization calls.

    Tier strategy (cheapest first):
      Level 1: Context Collapse  — Truncate oversized tool_result blocks
      Level 2: Micro-Compact      — Remove old tool_result content
      Level 3: Auto-Compaction    — LLM generates summary to replace old history
      Level 4: History Snip       — Force-drop oldest messages (last resort)

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
        on_progress: CompactProgressCallback | None = None,
        todo_tool: Any = None,
    ):
        self.fallback_manager = fallback_manager
        self.config = config or CompactionConfig()
        self._compaction_count = 0
        self._total_tokens_saved = 0
        self._consecutive_failures = 0
        self._on_progress = on_progress
        # Optional TodoTool reference — when present, its active items are
        # re-injected as a standalone user message after auto-compaction so
        # the model does not lose the plan across context compression.
        self._todo_tool = todo_tool

    def set_todo_tool(self, todo_tool: Any) -> None:
        """Attach (or replace) the todo tool used for post-compaction re-injection."""
        self._todo_tool = todo_tool

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "compaction_count": self._compaction_count,
            "total_tokens_saved": self._total_tokens_saved,
            "consecutive_failures": self._consecutive_failures,
        }

    def reset_stats(self) -> None:
        self._compaction_count = 0
        self._total_tokens_saved = 0
        self._consecutive_failures = 0

    async def _emit_progress(
        self,
        phase: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._on_progress:
            try:
                await self._on_progress(phase, message, details or {})
            except Exception as e:
                logger.debug("Progress callback error: {}", e)

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
        # Guard against small context windows where reserved_output_tokens
        # + auto_compact_buffer would otherwise push effective_max negative.
        # A negative threshold makes `current_tokens < threshold` always
        # False, which forces compaction on every turn and burns through
        # `_consecutive_failures` without doing anything useful.
        effective_max = max_tokens - cfg.reserved_output_tokens - cfg.auto_compact_buffer
        if effective_max <= 0:
            # Fall back to half the window — still leaves room for output
            # but avoids the spurious-compaction death spiral.
            logger.warning(
                "Compaction buffers (reserved_output={} + auto_compact_buffer={}) "
                "exceed max_tokens={}; falling back to max_tokens//2 as effective_max",
                cfg.reserved_output_tokens, cfg.auto_compact_buffer, max_tokens,
            )
            effective_max = max(1, max_tokens // 2)
        threshold = effective_max * cfg.threshold_ratio

        if current_tokens < threshold:
            return messages, CompactResult(
                action=CompactAction.NONE,
                messages_before=len(messages),
                messages_after=len(messages),
                tokens_before=current_tokens,
                tokens_after=current_tokens,
            )

        if self._consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            logger.warning(
                "Skipping compaction: {} consecutive failures reached",
                self._consecutive_failures,
            )
            return messages, CompactResult(
                action=CompactAction.NONE,
                messages_before=len(messages),
                messages_after=len(messages),
                tokens_before=current_tokens,
                tokens_after=current_tokens,
            )

        logger.info(
            "Tokens {} / {} (threshold {}), starting multi-level compaction",
            current_tokens, effective_max, int(threshold),
        )

        await self._emit_progress("compaction_start", "Starting context compaction", {
            "current_tokens": current_tokens,
            "threshold": int(threshold),
        })

        tokens_before = current_tokens
        msgs_before = len(messages)

        attachments = extract_attachments_from_messages(messages)

        messages = self._apply_collapse(messages)
        tokens_after_collapse = _estimate_messages_tokens(messages)
        logger.debug("After collapse: {} tokens (saved {})", tokens_after_collapse, tokens_before - tokens_after_collapse)

        if tokens_after_collapse < threshold:
            self._consecutive_failures = 0
            self._record_savings(tokens_before, tokens_after_collapse)
            await self._emit_progress("compaction_end", "Context collapse completed", {
                "action": "collapse",
                "tokens_saved": tokens_before - tokens_after_collapse,
            })
            return messages, CompactResult(
                action=CompactAction.COLLAPSE,
                messages_before=msgs_before,
                messages_after=len(messages),
                tokens_before=tokens_before,
                tokens_after=tokens_after_collapse,
                attachments=attachments,
            )

        messages = self._apply_micro_compact(messages)
        tokens_after_micro = _estimate_messages_tokens(messages)
        logger.debug("After micro-compact: {} tokens (saved {})", tokens_after_micro, tokens_before - tokens_after_micro)

        if tokens_after_micro < threshold:
            self._consecutive_failures = 0
            self._record_savings(tokens_before, tokens_after_micro)
            await self._emit_progress("compaction_end", "Micro-compact completed", {
                "action": "micro_compact",
                "tokens_saved": tokens_before - tokens_after_micro,
            })
            return messages, CompactResult(
                action=CompactAction.MICRO_COMPACT,
                messages_before=msgs_before,
                messages_after=len(messages),
                tokens_before=tokens_before,
                tokens_after=tokens_after_micro,
                attachments=attachments,
            )

        await self._emit_progress("auto_compact_start", "Generating conversation summary", {
            "messages_to_compact": msgs_before,
        })

        messages, summary = await self._apply_auto_compaction(messages, task_context=task_context)
        tokens_after_auto = _estimate_messages_tokens(messages)
        logger.debug("After auto-compaction: {} tokens (saved {})", tokens_after_auto, tokens_before - tokens_after_auto)

        if tokens_after_auto < threshold:
            self._consecutive_failures = 0
            self._record_savings(tokens_before, tokens_after_auto)
            messages = self._inject_attachments(messages, attachments)
            await self._emit_progress("compaction_end", "Auto-compaction completed", {
                "action": "auto_compact",
                "tokens_saved": tokens_before - tokens_after_auto,
            })
            return messages, CompactResult(
                action=CompactAction.AUTO_COMPACT,
                messages_before=msgs_before,
                messages_after=len(messages),
                tokens_before=tokens_before,
                tokens_after=tokens_after_auto,
                summary=summary,
                attachments=attachments,
            )

        self._consecutive_failures += 1
        messages = self._apply_history_snip(messages, task_context=task_context)
        tokens_after_snip = _estimate_messages_tokens(messages)
        self._record_savings(tokens_before, tokens_after_snip)
        messages = self._inject_attachments(messages, attachments)
        logger.warning(
            "Had to use history snip (last resort): {} -> {} tokens",
            tokens_before, tokens_after_snip,
        )
        await self._emit_progress("compaction_end", "History snip applied (last resort)", {
            "action": "history_snip",
            "tokens_saved": tokens_before - tokens_after_snip,
        })
        return messages, CompactResult(
            action=CompactAction.HISTORY_SNIP,
            messages_before=msgs_before,
            messages_after=len(messages),
            tokens_before=tokens_before,
            tokens_after=tokens_after_snip,
            attachments=attachments,
        )

    async def reactive_compact(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        error: Exception,
        task_context: str = "",
    ) -> tuple[list[dict[str, Any]], CompactResult] | None:
        """Attempt reactive compaction when the API returns a PTL error.

        Called when the LLM API rejects the prompt for being too long.
        Tries aggressive compaction to recover and allow a retry.

        Returns None if compaction cannot help (e.g., not a PTL error).
        """
        if not is_prompt_too_long_error(error):
            return None

        logger.warning(
            "Prompt-too-long error detected, attempting reactive compaction"
        )
        await self._emit_progress("reactive_compact", "Prompt too long; compacting and retrying", {
            "error": str(error)[:200],
        })

        current_tokens = _estimate_messages_tokens(messages)
        result = await self.maybe_compact(
            messages, current_tokens, max_tokens,
            task_context=task_context,
        )

        if result[1].action == CompactAction.NONE:
            truncated = truncate_head_for_ptl_retry(messages)
            if truncated is not None:
                tokens_after = _estimate_messages_tokens(truncated)
                logger.info(
                    "PTL retry: truncated head, {} -> {} tokens",
                    current_tokens, tokens_after,
                )
                return truncated, CompactResult(
                    action=CompactAction.HISTORY_SNIP,
                    messages_before=len(messages),
                    messages_after=len(truncated),
                    tokens_before=current_tokens,
                    tokens_after=tokens_after,
                )

        return result

    def _record_savings(self, before: int, after: int) -> None:
        self._compaction_count += 1
        self._total_tokens_saved += max(0, before - after)

    @staticmethod
    def _inject_attachments(
        messages: list[dict[str, Any]],
        attachments: list[CompactAttachment],
    ) -> list[dict[str, Any]]:
        """Inject compact attachments into the message list after compaction."""
        if not attachments:
            return messages
        attachment_messages = [att.to_message() for att in attachments]
        sys_idx = next(
            (i for i, m in enumerate(messages) if m.get("role") == "system"),
            None,
        )
        if sys_idx is not None:
            return messages[:sys_idx + 1] + attachment_messages + messages[sys_idx + 1:]
        return attachment_messages + messages

    # ------------------------------------------------------------------
    # Level 1: Context Collapse — truncate oversized blocks (head+tail)
    # ------------------------------------------------------------------

    def _apply_collapse(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cfg = self.config
        limit = cfg.collapse_tool_result_chars
        head = cfg.collapse_head_chars
        tail = cfg.collapse_tail_chars
        collapsed = 0
        for m in messages:
            content = m.get("content")

            if isinstance(content, str) and len(content) > limit:
                m["content"] = collapse_text_head_tail(
                    content, limit, head_chars=head, tail_chars=tail,
                )
                collapsed += 1
                continue

            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") not in ("tool_result", "text"):
                    continue
                key = "content" if block.get("type") == "tool_result" else "text"
                text = str(block.get(key, ""))
                if len(text) <= limit:
                    continue
                block[key] = collapse_text_head_tail(text, limit, head_chars=head, tail_chars=tail)
                collapsed += 1
        if collapsed:
            logger.info("Collapsed {} oversized blocks (head+tail)", collapsed)
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
            logger.info("Micro-compacted {} old tool results", cleared)
        if protected_count:
            logger.info("Protected {} task-related tool results from micro-compact", protected_count)
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

        split = max(0, len(messages) - keep_recent * 2)
        recent_messages = messages[split:]
        needed_ids: set[str] = set()
        for m in recent_messages:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                needed_ids.add(str(m["tool_call_id"]))
        if needed_ids:
            declared_in_recent: set[str] = set()
            for m in recent_messages:
                if m.get("role") == "assistant":
                    for tc in m.get("tool_calls") or []:
                        if isinstance(tc, dict) and tc.get("id"):
                            declared_in_recent.add(str(tc["id"]))
            missing = needed_ids - declared_in_recent
            if missing:
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
                    for i in range(earliest, max(0, earliest - 10), -1):
                        if messages[i].get("role") == "user":
                            split = i
                            break
                    else:
                        split = max(0, earliest)
        messages_to_compact = messages[:split]
        recent_messages = messages[split:]

        messages_to_compact = replace_images_with_placeholders(messages_to_compact)

        conversation_text = self._format_messages_for_compact(messages_to_compact)
        if task_context:
            conversation_text += f"\n\n{task_context}"
        try:
            summary = await self._generate_summary(conversation_text)
        except Exception as e:
            logger.error("Auto-compaction LLM failed: {}", e)
            self._consecutive_failures += 1
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
            "Auto-compacted: {} old messages summarized, {} recent kept",
            len(messages_to_compact), len(recent_messages),
        )
        new_messages = [compact_msg] + recent_messages

        # Re-inject the active todo list as a standalone user message so the
        # model sees the remaining plan verbatim after compaction, instead of
        # relying on the LLM summariser to preserve it. Mirrors Hermes's
        # ``TodoStore.format_for_injection`` post-compaction injection.
        # The todo tool is optional; skip silently if absent.
        todo_tool = getattr(self, "_todo_tool", None)
        if todo_tool is not None and hasattr(todo_tool, "format_for_injection"):
            try:
                snapshot = todo_tool.format_for_injection()
                if snapshot:
                    new_messages.insert(1, {
                        "role": "user",
                        "content": snapshot,
                        # Marker so downstream consumers can identify it
                        # as a synthetic re-injection (not a real user turn).
                        "_todo_reinjection": True,
                    })
                    logger.debug(
                        "Re-injected active todo snapshot after auto-compaction"
                    )
            except Exception as exc:
                logger.debug(
                    "Failed to re-inject todo snapshot after compaction: {}", exc
                )

        return new_messages, formatted

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
                        "History snip dropped orphan tool_result (id={})",
                        tid,
                    )
                    continue
            cleaned.append(msg)

        logger.warning(
            "History snip: {} -> {} messages (dropped {}, cleaned {} orphan tools)",
            len(messages), len(cleaned), len(messages) - keep, len(result) - len(cleaned),
        )
        return cleaned

    # ------------------------------------------------------------------
    # Summary generation (shared with legacy interface)
    # ------------------------------------------------------------------

    COMPACT_PROMPT = (
        "You are a summarization engine. Output ONLY <analysis> then <summary> blocks. "
        "You have NO tools. Do NOT write tool calls, JSON, or function syntax.\n\n"
        "Create a detailed summary of the conversation so far, capturing technical details, "
        "code patterns, and decisions essential for continuing work without losing context.\n\n"
        "Required sections in <summary>:\n"
        "1. Primary Request and Intent\n"
        "2. Key Technical Concepts\n"
        "3. Files and Code Sections (examined/modified/created)\n"
        "4. Errors and Fixes\n"
        "5. Problem Solving\n"
        "6. All User Messages (verbatim, not tool results)\n"
        "7. Pending Tasks (include ALL items from [CURRENT TASK STATE] verbatim if present)\n"
        "8. Current Work (what was in progress before this summary)\n"
        "9. Optional Next Step\n\n"
        "Output format: <analysis>your analysis</analysis> <summary>your summary</summary>"
    )

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
                            text_parts.append("[image omitted]")
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
                response, _ = await self.fallback_manager.chat_with_fallback(
                    messages=[
                        {"role": "system", "content": self.COMPACT_PROMPT},
                        {"role": "user", "content": conversation_text},
                    ],
                    max_tokens=self.config.max_compact_output_tokens,
                )
                if response.finish_reason == "error":
                    error_msg = response.content or "Unknown error"
                    logger.error("LLM returned error: {}", error_msg)
                    raise RuntimeError(f"Summary generation failed: {error_msg}")
                return response.content or "[Empty summary]"
            except Exception as e:
                logger.error("Failed to generate summary with provider: {}", e)
                raise

        logger.warning("No LLM available, using simple truncation summary")
        lines = conversation_text.split("\n\n")[:5]
        return "[Simple summary - first 5 message pairs]\n" + "\n\n".join(lines)

    def _simple_truncation_summary(self, messages: list[dict[str, Any]]) -> str:
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
        summary = re.sub(
            r'<function_calls?>[\s\S]*?</function_calls?>', '', summary,
        )
        summary = re.sub(
            r'\{\s*"tool_calls?"\s*:[\s\S]*?\}', '', summary,
        )
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
            formatted = re.sub(r'<analysis>[\s\S]*?</analysis>', '', summary)

        formatted = re.sub(r'\n{3,}', '\n\n', formatted).strip()
        return formatted
