"""ReMeLight-backed memory manager for markbot.

Ported from ReMeLightMemoryManager with full feature parity:
- Conversation compaction via compact_memory()
- Memory summarization with file tools via summary_memory()
- Vector and full-text search via memory_search()
- Tool result offload via compact_tool_result()
- Fine-grained configuration (language, timezone, compact_ratio, thinking_block)
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import platform
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from markbot.utils.constants import (
    MAX_COMPRESSED_SUMMARY_CHARS,
    MAX_DAILY_LOG_RESULT_CHARS,
    MAX_MEMORY_MD_CHARS,
)

from .base import BaseMemoryManager

if TYPE_CHECKING:
    from reme.memory.file_based.reme_in_memory_memory import ReMeInMemoryMemory

_EXPECTED_REME_VERSION = "0.3.1.8"


class _MessageWrapper:
    """Wrapper to convert dict messages to objects expected by reme-ai."""

    def __init__(
        self,
        role: str = "",
        content: str = "",
        name: str = "",
        timestamp: str = "",
        metadata: dict | None = None,
    ):
        self.role = role
        self.content = content
        self.name = name
        self.timestamp = timestamp
        self.metadata = metadata or {}

    _DEFAULT_USER_NAME: str = "user"

    @classmethod
    def from_dict(cls, msg: dict) -> "_MessageWrapper":
        role = msg.get("role", "")
        if role == "user":
            name = cls._DEFAULT_USER_NAME
        else:
            name = msg.get("name", "")
        content = msg.get("content")
        if content is None:
            content = ""
        return cls(
            role=role,
            content=content,
            name=name,
            timestamp=msg.get("timestamp", ""),
            metadata=msg.get("metadata", {}),
        )

    def get_content_blocks(self, block_type: str | None = None) -> list[dict]:
        """Return content as a list of content blocks for reme-ai compatibility.

        Returns a list of dict blocks. If content is a string, wraps it in a text block.
        If content is already a list, returns it as-is.

        Args:
            block_type: Optional filter to return only blocks of this type
                (e.g. "tool_use", "tool_result", "text").  When ``None``,
                all blocks are returned – this preserves the original behaviour.
        """
        if isinstance(self.content, str):
            blocks = [{"type": "text", "text": self.content}]
        elif isinstance(self.content, list):
            blocks = self.content
        else:
            blocks = [{"type": "text", "text": str(self.content)}]

        if block_type is not None:
            blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == block_type]

        return blocks


class ReMeLightMemoryManager(BaseMemoryManager):
    """Memory manager wrapping ReMeLight for markbot via composition.

    Holds a ``ReMeLight`` instance and delegates all lifecycle / search /
    compaction calls to it.

    Capabilities:
    - Conversation compaction via compact_memory()
    - Memory summarization with file tools via summary_memory()
    - Vector and full-text search via memory_search()
    - Tool result offload via compact_tool_result()
    """

    def __init__(
        self,
        working_dir: str,
        agent_id: str = "default",
        fallback_manager=None,
        model: str | None = None,
        embedding_config: dict | None = None,
        llm_config: dict | None = None,
        language: str = "zh",
        timezone: str | None = None,
        context_compact_enabled: bool = True,
        memory_compact_ratio: float = 0.75,
        memory_reserve_ratio: float = 0.1,
        compact_with_thinking_block: bool = True,
        memory_summary_enabled: bool = True,
        force_memory_search: bool = False,
        force_max_results: int = 1,
        force_min_score: float = 0.3,
        tool_result_compact_enabled: bool = True,
        tool_result_recent_n: int = 2,
        tool_result_old_max_bytes: int = 3000,
        tool_result_recent_max_bytes: int = 50000,
        tool_result_retention_days: int = 5,
        max_input_length: int = 131072,
    ):
        import time
        _init_start = time.time()
        logger.info("[ReMeLightMemoryManager] Starting initialization...")

        super().__init__(working_dir=working_dir, agent_id=agent_id)
        self._fallback_manager = fallback_manager
        self._model = model
        self._embedding_config = embedding_config or {}
        self._llm_config = llm_config or {}
        self._language = language
        self._timezone = timezone

        self.context_compact_enabled = context_compact_enabled
        self.memory_compact_ratio = memory_compact_ratio
        self.memory_reserve_ratio = memory_reserve_ratio
        self.compact_with_thinking_block = compact_with_thinking_block
        self.memory_summary_enabled = memory_summary_enabled
        self.force_memory_search = force_memory_search
        self.force_max_results = force_max_results
        self.force_min_score = force_min_score

        self.tool_result_compact_enabled = tool_result_compact_enabled
        self.tool_result_recent_n = tool_result_recent_n
        self.tool_result_old_max_bytes = tool_result_old_max_bytes
        self.tool_result_recent_max_bytes = tool_result_recent_max_bytes
        self.tool_result_retention_days = tool_result_retention_days
        self.max_input_length = max_input_length

        self._reme_version_ok: bool = self._check_reme_version()
        self._reme = None
        self._started = False
        self._compressed_summary_dir = Path(working_dir) / "memory"
        self._compressed_summary_dir.mkdir(parents=True, exist_ok=True)
        self._session_summaries: dict[str, str] = {}
        self._compressed_summary_path = self._compressed_summary_dir / ".compressed_summary"
        self._compressed_summary: str = self._load_compressed_summary()
        self._daily_log_manager: Any = None
        self._summary_toolkit: Any = None

        logger.info(
            "[ReMeLightMemoryManager] Config: agent_id={}, working_dir={}, language={}",
            agent_id, working_dir, language
        )

        self._init_reme()
        logger.info("[ReMeLightMemoryManager] Initialization complete, total took {:.3f}s", time.time() - _init_start)

    def _init_reme(self) -> None:
        import time
        _init_reme_start = time.time()
        logger.info("[ReMeLightMemoryManager] _init_reme starting...")

        backend_env = os.environ.get("MEMORY_STORE_BACKEND", "auto")
        if backend_env == "auto":
            if platform.system() == "Windows":
                memory_backend = "local"
            else:
                try:
                    import chromadb
                    memory_backend = "chroma"
                except Exception as e:
                    logger.warning(
                        "[ReMeLightMemoryManager] chromadb import failed, falling back to `local` backend. Error: {}",
                        e
                    )
                    memory_backend = "local"
        else:
            memory_backend = backend_env
        logger.info("[ReMeLightMemoryManager] Memory backend: {}", memory_backend)

        _t0 = time.time()
        try:
            from reme.reme_light import ReMeLight
        except ImportError:
            logger.error(
                "[ReMeLightMemoryManager] reme-ai is not installed. Memory system requires reme-ai. "
                "Install with: pip install reme-ai"
            )
            self._reme = None
            return
        logger.debug("[ReMeLightMemoryManager] Import ReMeLight took {:.3f}s", time.time() - _t0)

        emb_config = self._build_embedding_config()
        vector_enabled = bool(emb_config.get("base_url")) and bool(
            emb_config.get("model_name")
        )

        log_cfg = {**emb_config, "api_key": self._mask_key(emb_config.get("api_key", ""))}
        logger.info(
            "[ReMeLightMemoryManager] Embedding config: %s, vector_enabled=%s, memory_backend=%s",
            json.dumps(log_cfg, ensure_ascii=False), vector_enabled, memory_backend,
        )

        fts_enabled = os.environ.get("FTS_ENABLED", "1").lower() not in ("0", "false", "no", "off")

        llm_api_key = self._llm_config.get("api_key") or os.environ.get("AS_LLM_API_KEY", "")
        llm_base_url = self._llm_config.get("base_url") or os.environ.get("AS_LLM_BASE_URL", "")
        llm_model_name = self._llm_config.get("model_name") or os.environ.get("AS_LLM_MODEL_NAME", "")
        llm_backend = self._llm_config.get("backend", "openai")

        default_as_llm_config = None
        if llm_api_key and llm_base_url and llm_model_name:
            default_as_llm_config = {
                "backend": llm_backend,
                "model_name": llm_model_name,
            }

        os.environ.setdefault("AS_TOKEN_COUNTER_BACKEND", "huggingface")
        os.environ.setdefault("AS_TOKEN_COUNTER_MODEL_NAME", "gpt2")

        _t0 = time.time()
        logger.info("[ReMeLightMemoryManager] Creating ReMeLight instance...")
        
        import logging as _std_logging
        _reme_logger = _std_logging.getLogger('reme')
        _old_level = _reme_logger.level
        _reme_logger.setLevel(_std_logging.WARNING)
        
        try:
            self._reme = ReMeLight(
                working_dir=self.working_dir,
                llm_api_key=llm_api_key or None,
                llm_base_url=llm_base_url or None,
                default_as_llm_config=default_as_llm_config,
                default_embedding_model_config=emb_config,
                default_file_store_config={
                    "backend": memory_backend,
                    "store_name": "markbot",
                    "vector_enabled": vector_enabled,
                    "fts_enabled": fts_enabled,
                },
                default_file_watcher_config={
                    "rebuild_index_on_start": False,
                },
            )
        finally:
            _reme_logger.setLevel(_old_level)
        
        logger.info("[ReMeLightMemoryManager] ReMeLight instance created, took {:.3f}s", time.time() - _t0)

        self._setup_summary_toolkit()
        logger.info("[ReMeLightMemoryManager] _init_reme complete, total took {:.3f}s", time.time() - _init_reme_start)

    def _setup_summary_toolkit(self) -> None:
        """Register file tools for use during summarization."""
        try:
            from agentscope.tool import Toolkit

            from markbot.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool

            self._summary_toolkit = Toolkit()
            workspace = Path(self.working_dir)

            read_tool = ReadFileTool(workspace=workspace)
            write_tool = WriteFileTool(workspace=workspace)
            edit_tool = EditFileTool(workspace=workspace)

            self._summary_toolkit.register_tool_function(read_tool.execute)
            self._summary_toolkit.register_tool_function(write_tool.execute)
            self._summary_toolkit.register_tool_function(edit_tool.execute)

            logger.info("Summary toolkit registered with read/write/edit file tools")
        except Exception as e:
            logger.warning("Failed to setup summary toolkit: %s", e)
            self._summary_toolkit = None

    def _build_embedding_config(self) -> dict:
        cfg = self._embedding_config
        return {
            "backend": cfg.get("backend", "openai"),
            "api_key": cfg.get("api_key") or os.environ.get("EMBEDDING_API_KEY", ""),
            "base_url": cfg.get("base_url") or os.environ.get("EMBEDDING_BASE_URL", ""),
            "model_name": cfg.get("model_name") or os.environ.get("EMBEDDING_MODEL_NAME", ""),
            "dimensions": cfg.get("dimensions"),
            "enable_cache": cfg.get("enable_cache", True),
            "use_dimensions": cfg.get("use_dimensions", False),
            "max_cache_size": cfg.get("max_cache_size", 2000),
            "max_input_length": cfg.get("max_input_length", 8192),
            "max_batch_size": cfg.get("max_batch_size", 100),
        }

    @staticmethod
    def _mask_key(key: str) -> str:
        return key[:5] + "*" * (len(key) - 5) if len(key) > 5 else key

    @staticmethod
    def _check_reme_version() -> bool:
        try:
            installed = importlib.metadata.version("reme-ai")
        except importlib.metadata.PackageNotFoundError:
            return True
        if installed != _EXPECTED_REME_VERSION:
            logger.warning(
                f"reme-ai version mismatch: installed={installed}, "
                f"expected={_EXPECTED_REME_VERSION}. "
                f"Run `pip install reme-ai=={_EXPECTED_REME_VERSION}` to align."
            )
            return False
        return True

    def _warn_if_version_mismatch(self) -> None:
        if not self._reme_version_ok:
            logger.warning(
                "[ReMeLightMemoryManager] reme-ai version mismatch, expected={}. "
                "Run `pip install reme-ai=={}` to align.",
                _EXPECTED_REME_VERSION, _EXPECTED_REME_VERSION
            )

    def _prepare_model_formatter(self) -> None:
        pass

    async def start(self):
        import time
        _start_begin = time.time()
        logger.info("[ReMeLightMemoryManager] start() beginning...")

        self._warn_if_version_mismatch()
        if self._reme is None:
            logger.warning("[ReMeLightMemoryManager] Cannot start memory manager: _reme is None")
            return None
        if self._summary_toolkit is None:
            logger.warning(
                "[ReMeLightMemoryManager] Summary toolkit not available (agentscope not installed). "
                "summary_memory() will run without file tool support."
            )
        try:
            logger.info("[ReMeLightMemoryManager] Calling _reme.start()...")
            _t0 = time.time()
            result = await self._reme.start()
            logger.info("[ReMeLightMemoryManager] _reme.start() took {:.3f}s", time.time() - _t0)
            self._started = True
            logger.info("[ReMeLightMemoryManager] Started successfully, working_dir=%s", self.working_dir)
            memory_dir = Path(self.working_dir) / "memory"
            logger.info("[ReMeLightMemoryManager] Memory directory: %s, exists=%s", memory_dir, memory_dir.exists())
            logger.info("[ReMeLightMemoryManager] start() complete, total took {:.3f}s", time.time() - _start_begin)
            return result
        except Exception as e:
            logger.error("[ReMeLightMemoryManager] Failed to start: %s", e)
            return None

    async def close(self) -> bool:
        logger.info(f"MemoryManager closing: working_dir={self.working_dir}")
        if self._reme is None:
            return True
        result = await self._reme.close()
        self._started = False
        logger.info(f"MemoryManager closed: result={result}")
        return result

    async def compact_tool_result(self, **kwargs):
        if self._reme is None:
            return None
        messages = kwargs.get("messages", [])
        if messages and isinstance(messages, list):
            wrapped_messages = [
                _MessageWrapper.from_dict(m) if isinstance(m, dict) else m
                for m in messages
                if isinstance(m, dict) and "role" in m
            ]
            kwargs["messages"] = wrapped_messages
        return await self._reme.compact_tool_result(**kwargs)

    async def check_context(self, **kwargs):
        if self._reme is None:
            return None
        messages = kwargs.get("messages", [])
        if messages and isinstance(messages, list):
            wrapped_messages = [
                _MessageWrapper.from_dict(m) if isinstance(m, dict) else m
                for m in messages
                if isinstance(m, dict) and "role" in m
            ]
            kwargs["messages"] = wrapped_messages

        result = await self._reme.check_context(**kwargs)

        # Handle error case: result might be a string if an exception occurred
        if isinstance(result, str):
            logger.warning(f"check_context returned error: {result}")
            return None

        if result is None or len(result) < 3:
            return result

        # Convert Msg objects to dicts
        def msg_to_dict(msg):
            if hasattr(msg, 'to_dict'):
                return msg.to_dict()
            elif isinstance(msg, dict):
                return msg
            else:
                return {
                    'role': getattr(msg, 'role', ''),
                    'content': getattr(msg, 'content', ''),
                    'name': getattr(msg, 'name', ''),
                    'timestamp': getattr(msg, 'timestamp', ''),
                    'metadata': getattr(msg, 'metadata', {}),
                }

        messages_to_compact, messages_to_keep, is_valid = result[0], result[1], result[2]

        if messages_to_compact and isinstance(messages_to_compact, list):
            messages_to_compact = [msg_to_dict(m) for m in messages_to_compact]
        if messages_to_keep and isinstance(messages_to_keep, list):
            messages_to_keep = [msg_to_dict(m) for m in messages_to_keep]

        return messages_to_compact, messages_to_keep, is_valid

    async def compact_memory(
        self,
        messages: list,
        previous_summary: str = "",
        extra_instruction: str = "",
        **kwargs,
    ) -> str:
        if self._reme is None:
            return ""

        messages = [m for m in messages if isinstance(m, dict) and "role" in m]
        wrapped_messages = [_MessageWrapper.from_dict(m) for m in messages]

        compact_params = {
            "messages": wrapped_messages,
            "previous_summary": previous_summary,
            "return_dict": True,
            "language": self._language,
            "max_input_length": self.max_input_length,
            "compact_ratio": self.memory_compact_ratio,
            "add_thinking_block": self.compact_with_thinking_block,
        }

        if extra_instruction:
            compact_params["extra_instruction"] = extra_instruction

        result = await self._reme.compact_memory(**compact_params)

        if isinstance(result, str):
            logger.error(f"compact_memory returned str instead of dict: {result[:200]}...")
            return result

        if not result.get("is_valid", True):
            unique_id = uuid.uuid4().hex[:8]
            debug_dir = os.path.join(self.working_dir, "memory")
            os.makedirs(debug_dir, exist_ok=True)
            filepath = os.path.join(debug_dir, f"compact_invalid_{unique_id}.json")
            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                logger.error(
                    f"Invalid compact result saved to {filepath}. "
                    f"user_msg: {result.get('user_message', '')[:200]}..., "
                    f"history_compact: {result.get('history_compact', '')[:200]}..."
                )
            except Exception:
                logger.error("Failed to save invalid compact result")
            return ""

        return result.get("history_compact", "")

    async def summary_memory(self, messages: list, **kwargs) -> str:
        if self._reme is None:
            logger.warning("Memory manager not initialized (_reme is None), skipping summary")
            return ""
        if not self._started:
            logger.warning("Memory manager not started (_started is False), skipping summary")
            return ""

        try:
            wrapped_messages = [_MessageWrapper.from_dict(m) for m in messages]

            summary_params = {
                "messages": wrapped_messages,
                "language": self._language,
                "max_input_length": self.max_input_length,
                "compact_ratio": self.memory_compact_ratio,
                "timezone": self._timezone,
                "add_thinking_block": self.compact_with_thinking_block,
            }

            if self._summary_toolkit is not None:
                summary_params["toolkit"] = self._summary_toolkit

            result = await self._reme.summary_memory(**summary_params)
            logger.info(f"Summary memory completed, result length: {len(result) if result else 0}")
            return result
        except Exception as e:
            logger.error(f"Summary memory failed: {e}")
            return ""

    async def memory_search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict]:
        results: list[dict] = []

        if self._reme is not None and self._started:
            try:
                result = await self._reme.memory_search(
                    query=query,
                    max_results=max_results,
                    min_score=min_score,
                )
                if hasattr(result, "content"):
                    for block in result.content:
                        if hasattr(block, "text"):
                            results.append({"content": block.text, "source": "reme"})
                        elif isinstance(block, dict) and "text" in block:
                            results.append({"content": block["text"], "source": "reme"})
                elif isinstance(result, str):
                    results.append({"content": result, "source": "reme"})
                elif isinstance(result, list):
                    results.extend({"content": str(r), "source": "reme"} for r in result)
                else:
                    results.append({"content": str(result), "source": "reme"})
            except Exception as e:
                logger.error(f"Memory search failed: {e}")

        # Also try daily log keyword search for complementary results
        loop = asyncio.get_running_loop()
        daily_results = await loop.run_in_executor(
            None, self._search_daily_logs, query, max_results, channel, chat_id
        )
        for r in daily_results:
            r.setdefault("source", "daily_log")

        # Merge: ReMe results first, then deduplicated daily log results
        existing_contents = {r.get("content", "")[:200] for r in results}
        for r in daily_results:
            key = r.get("content", "")[:200]
            if key not in existing_contents and len(results) < max_results * 2:
                results.append(r)
                existing_contents.add(key)

        # If still no results, try chronological retrieval from daily logs
        if not results and self._daily_log_manager is not None:
            recent_msgs = self._daily_log_manager.get_recent_user_messages(
                limit=max_results * 2,
                channel=channel,
                chat_id=chat_id,
            )
            if recent_msgs:
                lines = ["## Recent User Messages (chronological)\n"]
                for i, m in enumerate(recent_msgs, 1):
                    lines.append(f"{i}. [{m['timestamp']}] {m['content'][:500]}")
                results.append({
                    "content": "\n".join(lines),
                    "source": "daily_log/recent",
                })

        return results

    async def retrieve(
        self,
        messages: list[dict],
        *,
        channel: str | None = None,
        chat_id: str | None = None,
        **kwargs,
    ) -> str | None:
        """Retrieve relevant memory and return formatted context text.

        Builds a query from the latest messages, searches memory, and
        returns a formatted string that can be injected into the system
        prompt by the caller.
        """
        if not messages:
            return None

        query_parts: list[str] = []
        total = 0
        for msg in reversed(messages):
            remaining = 100 - total
            if remaining <= 0:
                break
            content = msg.get("content", "")
            if content is None:
                content = ""
            if isinstance(content, list):
                text = ""
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        block_text = block.get("text", "")
                        if block_text is None:
                            block_text = ""
                        text += block_text
                content = text
            content = (content or "").strip()
            if not content:
                continue
            chunk = content[:remaining]
            query_parts.insert(0, chunk)
            total += len(chunk)

        query = " ".join(query_parts).strip()
        if not query:
            return None

        try:
            results = await self.memory_search(
                query=query,
                max_results=3,
                min_score=0.15,
                channel=channel,
                chat_id=chat_id,
            )
        except Exception as e:
            logger.warning(f"[MemoryManager] retrieve() search failed: {e}")
            return None

        if not results:
            return None

        text_parts: list[str] = []
        for idx, r in enumerate(results, 1):
            content = r.get("content", "")
            if content is None:
                content = ""
            source = r.get("source", "memory")
            if len(content) > 1500:
                content = content[:1500] + "\n... [truncated]"
            text_parts.append(f"### {idx}. [{source}]\n{content}")

        text_content = "\n\n".join(text_parts)
        if not text_content.strip():
            return None

        return text_content

    @staticmethod
    def _tokenize_for_search(text: str) -> list[str]:
        tokens = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", text.lower())
        bigrams = []
        for i in range(len(tokens) - 1):
            if tokens[i][0] >= '\u4e00' or tokens[i + 1][0] >= '\u4e00':
                bigrams.append(tokens[i] + tokens[i + 1])
        return tokens + bigrams

    def _search_daily_logs(
        self,
        query: str,
        max_results: int = 5,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict]:
        """Keyword search over workspace/memory/daily/*.md files.

        Uses token matching with CJK bigram support (split query into
        words/CJK chars + CJK bigrams, count hits per file section).
        This is a lightweight fallback — ReMeLight does not index the
        ``memory/daily/`` subdirectory.

        When *channel* and/or *chat_id* are provided, only log sections
        belonging to that session are considered.
        """
        if self._daily_log_manager is not None:
            return self._daily_log_manager.search(
                query,
                max_results,
                channel=channel,
                chat_id=chat_id,
            )

        daily_dir = Path(self.working_dir) / "memory" / "daily"
        if not daily_dir.is_dir():
            return []

        query_tokens = set(self._tokenize_for_search(query))
        if not query_tokens:
            return []

        candidates: list[tuple[float, str, str]] = []
        md_files = sorted(daily_dir.glob("*.md"), reverse=True)

        for md_file in md_files:
            try:
                text = md_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            sections = re.split(r"^## \[", text, flags=re.MULTILINE)
            for section in sections:
                if not section.strip():
                    continue

                header_line = section.split("\n", 1)[0]
                if channel or chat_id:
                    from .daily_log import _SECTION_HEADER_RE
                    match = _SECTION_HEADER_RE.match("## [" + header_line)
                    if match:
                        sec_channel = match.group("channel") or ""
                        sec_chat_id = match.group("chat_id") or ""
                        if channel and sec_channel != channel:
                            continue
                        if chat_id and sec_chat_id != chat_id:
                            continue

                section_text = section.lower()
                section_tokens = set(self._tokenize_for_search(section_text))
                hits = sum(1 for t in query_tokens if t in section_tokens)
                if hits == 0:
                    continue
                score = hits / len(query_tokens)
                section_len = max(len(section_text), 1)
                score = score * (1.0 / (1.0 + section_len / 10000.0))
                header = header_line[:80]
                content = section.strip()
                if len(content) > MAX_DAILY_LOG_RESULT_CHARS:
                    content = content[:MAX_DAILY_LOG_RESULT_CHARS] + "\n... [truncated]"
                candidates.append((score, header, content))

        candidates.sort(key=lambda x: x[0], reverse=True)

        results: list[dict] = []
        for score, header, content in candidates[:max_results]:
            results.append({
                "content": content,
                "source": f"daily/{header}",
                "score": round(score, 3),
            })

        return results

    def get_in_memory_memory(self, **kwargs) -> Optional["ReMeInMemoryMemory"]:
        if self._reme is None:
            return None
        return self._reme.get_in_memory_memory()

    def get_compressed_summary(self, *, session_key: str | None = None) -> str:
        if session_key:
            return self._session_summaries.get(session_key, "")
        return self._compressed_summary

    def set_compressed_summary(self, summary: str, *, session_key: str | None = None) -> None:
        if session_key:
            self._session_summaries[session_key] = self._truncate_summary(summary)
            self._save_session_summary(session_key, self._session_summaries[session_key])
        else:
            self._compressed_summary = self._truncate_summary(summary)
            self._save_compressed_summary(self._compressed_summary)

    _MAX_COMPRESSED_SUMMARY_CHARS = MAX_COMPRESSED_SUMMARY_CHARS

    def _truncate_summary(self, summary: str) -> str:
        if len(summary) <= self._MAX_COMPRESSED_SUMMARY_CHARS:
            return summary
        logger.warning(
            f"[MemoryManager] compressed_summary truncated: "
            f"{len(summary)} -> {self._MAX_COMPRESSED_SUMMARY_CHARS} chars"
        )
        return summary[:self._MAX_COMPRESSED_SUMMARY_CHARS] + "\n\n... [truncated to fit context window]"

    def _load_compressed_summary(self) -> str:
        if self._compressed_summary_path.exists():
            try:
                return self._compressed_summary_path.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return ""

    def _save_compressed_summary(self, summary: str) -> None:
        try:
            self._compressed_summary_path.parent.mkdir(parents=True, exist_ok=True)
            self._compressed_summary_path.write_text(summary, encoding="utf-8")
        except Exception as e:
            logger.warning(f"[MemoryManager] Failed to persist compressed_summary: {e}")

    def _session_summary_path(self, session_key: str) -> Path:
        safe_key = re.sub(r'[^\w\-.]', '_', session_key)
        return self._compressed_summary_dir / f".compressed_summary_{safe_key}"

    def _load_session_summary(self, session_key: str) -> str:
        path = self._session_summary_path(session_key)
        if path.exists():
            try:
                return path.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return ""

    def _save_session_summary(self, session_key: str, summary: str) -> None:
        try:
            path = self._session_summary_path(session_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(summary, encoding="utf-8")
        except Exception as e:
            logger.warning(f"[MemoryManager] Failed to persist session summary for {session_key}: {e}")

    _MAX_MEMORY_MD_CHARS = MAX_MEMORY_MD_CHARS

    async def get_memory_context(
        self,
        query: str | None = None,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> str:
        """Retrieve relevant memory for a query via semantic search.

        Only performs on-demand semantic search — does NOT inject session
        summaries or MEMORY.md content.  Session summaries are an internal
        compaction detail (used by MemoryCompactionHook for incremental
        compaction) and should not be re-exposed as context.  MEMORY.md is
        loaded during bootstrap by ContextBuilder and does not need to be
        duplicated here.

        Returns empty string when no query is provided or no results are found.
        """
        if not query:
            return ""

        if not self._started or self._reme is None:
            return ""

        try:
            search_results = await self.memory_search(
                query=query,
                max_results=5,
                min_score=0.2,
                channel=channel,
                chat_id=chat_id,
            )
        except Exception as e:
            logger.debug(f"memory_search in get_memory_context failed: {e}")
            return ""

        if not search_results:
            return ""

        relevant_parts: list[str] = []
        for r in search_results:
            text = r.get("content", "")
            if text is None:
                text = ""
            if text:
                if len(text) > 1500:
                    text = text[:1500] + "\n... [truncated]"
                relevant_parts.append(text)

        if not relevant_parts:
            return ""

        relevant_text = "\n\n---\n\n".join(relevant_parts)
        return f"## Relevant Memory (query: {query[:80]})\n\n{relevant_text}"

    @staticmethod
    def _truncate_by_section(content: str, max_chars: int) -> str:
        if len(content) <= max_chars:
            return content

        sections = []
        current_section = []
        current_len = 0

        for line in content.split("\n"):
            if line.startswith("## ") and current_section:
                section_text = "\n".join(current_section)
                if current_len + len(section_text) > max_chars:
                    break
                sections.append(section_text)
                current_len += len(section_text)
                current_section = [line]
            else:
                current_section.append(line)

        if current_section and current_len + len("\n".join(current_section)) <= max_chars:
            sections.append("\n".join(current_section))

        if not sections:
            first_section_end = content.find("\n## ")
            if first_section_end > 0:
                return content[:min(first_section_end, max_chars)] + "\n\n... [truncated]"
            return content[:max_chars] + "\n\n... [truncated]"

        return "\n\n".join(sections) + "\n\n... [remaining sections omitted]"

    def list_memory_entries(self) -> list[dict]:
        """List all memory entries for context explorer catalog.

        Returns a list of memory sources with metadata for display
        in the explore_context_catalog tool. This enables AI-driven
        dynamic loading of relevant context.
        """
        from datetime import datetime

        entries = []

        memory_dir = Path(self.working_dir) / "memory"

        if not memory_dir.exists():
            return entries

        memory_md = memory_dir / "MEMORY.md"
        if memory_md.exists():
            try:
                stat = memory_md.stat()
                content = memory_md.read_text(encoding='utf-8')
                preview_lines = content.split('\n')[:3]
                preview = '\n'.join(preview_lines)[:200]

                entries.append({
                    'title': 'MEMORY.md',
                    'source': 'memory',
                    'date': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d'),
                    'preview': preview,
                    'content': content,
                    'size_kb': round(stat.st_size / 1024, 1),
                })
            except Exception as e:
                logger.warning(f"Failed to read MEMORY.md for catalog: {e}")

        daily_logs_dir = memory_dir / "daily"
        if daily_logs_dir.exists():
            try:
                log_files = sorted(
                    daily_logs_dir.glob("*.md"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True
                )[:5]

                for log_file in log_files:
                    try:
                        stat = log_file.stat()
                        content = log_file.read_text(encoding='utf-8')
                        preview_lines = content.split('\n')[:3]
                        preview = '\n'.join(preview_lines)[:200]

                        entries.append({
                            'title': f'Daily Log: {log_file.stem}',
                            'source': 'memory',
                            'date': log_file.stem,
                            'preview': preview,
                            'content': content,
                            'size_kb': round(stat.st_size / 1024, 1),
                        })
                    except Exception as e:
                        logger.warning(f"Failed to read daily log {log_file}: {e}")
            except Exception as e:
                logger.warning(f"Failed to list daily logs: {e}")

        return entries

    _DREAM_PROMPT = """\
Enter dream state for memory optimization. Read today's logs and existing long-term memory, extract high-value incremental information, deduplicate and merge, and ultimately overwrite `MEMORY.md`. Ensure the long-term memory file remains up-to-date, concise, and non-redundant.

Current date: {current_date}

[Dream Optimization Principles]
1. Extreme Minimalism: Strictly forbid recording daily routines, specific bug-fix details, or one-off tasks. Retain ONLY 'core business decisions', 'confirmed user preferences', and 'high-value reusable experiences'.
2. State Overwrite: If a state change is detected (e.g., tech stack changes, config updates), you MUST replace the old state with the new one. Contradictory old and new information must not coexist.
3. Inductive Consolidation: Proactively distill and merge fragmented, similar rules into highly universal, independent entries.
4. Deprecation: Proactively delete hypotheses that have been proven false or outdated entries that no longer apply.

[Dream Execution Steps]
Step 1 [Load]: Invoke the `read` tool to read `MEMORY.md` in the root directory and today's log file `memory/daily/YYYY-MM-DD.md`.
Step 2 [Dream Purification]: Compare the old and new content. Strictly follow the [Dream Optimization Principles] to deduplicate, replace, remove, and merge, generating entirely new memory content.
Step 3 [Save]: Invoke the `write` or `edit` tool to overwrite the newly organized Markdown content into `MEMORY.md` (maintain clear hierarchy and list structures).
Step 4 [Awake Report]: After waking from your dream, briefly report: 1) What core memories were newly added/consolidated; 2) What outdated content was corrected/deleted."""

    async def dream(self, **kwargs) -> None:
        """Run one dream-based memory optimization pass.

        Creates a ReActAgent with file-editing tools to consolidate
        redundant or outdated entries in MEMORY.md.
        """
        logger.info("[Dream] Starting dream-based memory optimization")

        if self._summary_toolkit is None:
            logger.warning("[Dream] Summary toolkit not available, skipping dream")
            return

        try:
            from agentscope.agent import ReActAgent
            from agentscope.message import Msg, TextBlock
        except ImportError:
            logger.warning("[Dream] agentscope not available, skipping dream")
            return

        chat_model = None
        formatter = None
        if self._llm_config:
            try:
                provider_cfg = self._llm_config
                api_key = provider_cfg.get("api_key", "")
                base_url = provider_cfg.get("base_url", "")
                model_name = provider_cfg.get("model_name", "")

                if api_key and base_url and model_name:
                    from agentscope.model import OpenAIChatModel

                    chat_model = OpenAIChatModel(
                        model_name=model_name,
                        api_key=api_key,
                        client_kwargs={"base_url": base_url},
                    )
                    from agentscope.formatter import OpenAIChatFormatter

                    formatter = OpenAIChatFormatter()
            except Exception as e:
                logger.warning(f"[Dream] Failed to create model: {e}")
                return

        if chat_model is None:
            logger.warning("[Dream] No model available, skipping dream")
            return

        current_date = datetime.now().strftime("%Y-%m-%d")
        query_text = self._DREAM_PROMPT.format(current_date=current_date)

        if not query_text.strip():
            logger.debug("[Dream] Empty query, skipping")
            return

        backup_path = Path(self.working_dir).absolute() / "backup"
        backup_path.mkdir(parents=True, exist_ok=True)

        memory_file = Path(self.working_dir) / "MEMORY.md"
        if memory_file.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_filename = f"memory_backup_{timestamp}.md"
            backup_file = backup_path / backup_filename
            try:
                import shutil

                shutil.copyfile(memory_file, backup_file)
                logger.info(f"[Dream] Created MEMORY.md backup: {backup_file}")
            except Exception as e:
                logger.error(f"[Dream] Failed to create MEMORY.md backup: {e}, aborting dream")
                return
        else:
            logger.debug("[Dream] No existing MEMORY.md file to backup")

        dream_agent = ReActAgent(
            name="DreamOptimizer",
            model=chat_model,
            sys_prompt="You are a Dream Memory Organizer specialized"
            " in optimizing long-term memory files.",
            toolkit=self._summary_toolkit,
            formatter=formatter,
        )
        dream_agent.set_console_output_enabled(False)

        user_msg = Msg(
            name="dream",
            role="user",
            content=[TextBlock(type="text", text=query_text)],
        )

        try:
            response = await dream_agent.reply(user_msg)
            logger.info(f"[Dream] Optimization completed: {response.get_text_content()[:200]}...")
        except Exception as e:
            logger.error(f"[Dream] Memory optimization failed: {e}")
            raise

    async def restart_embedding_model(self):
        if self._reme is None:
            return
        await self._reme.restart(
            restart_config={
                "embedding_models": {"default": self._build_embedding_config()},
            },
        )
