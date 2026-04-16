"""ReMeLight-backed memory manager for markbot.

Ported from CoPaw's ReMeLightMemoryManager with full feature parity:
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
import logging
import os
import platform
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

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

    @classmethod
    def from_dict(cls, msg: dict) -> "_MessageWrapper":
        return cls(
            role=msg.get("role", ""),
            content=msg.get("content", ""),
            name=msg.get("name", ""),
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
        self._compressed_summary_path = Path(working_dir) / "memory" / ".compressed_summary"
        self._compressed_summary: str = self._load_compressed_summary()
        self._summary_toolkit: Any = None

        logger.info(
            f"ReMeLightMemoryManager init: agent_id={agent_id}, "
            f"working_dir={working_dir}, language={language}"
        )

        self._init_reme()

    def _init_reme(self) -> None:
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
                        f"chromadb import failed, falling back to `local` backend. Error: {e}"
                    )
                    memory_backend = "local"
        else:
            memory_backend = backend_env

        try:
            from reme.reme_light import ReMeLight
        except ImportError:
            logger.error(
                "reme-ai is not installed. Memory system requires reme-ai. "
                "Install with: pip install reme-ai"
            )
            self._reme = None
            return

        emb_config = self._build_embedding_config()
        vector_enabled = bool(emb_config.get("base_url")) and bool(
            emb_config.get("model_name")
        )

        log_cfg = {**emb_config, "api_key": self._mask_key(emb_config.get("api_key", ""))}
        logger.info(
            "Embedding config: %s, vector_enabled=%s, memory_backend=%s",
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

        # Set environment variables for token counter before initializing ReMeLight
        os.environ.setdefault("AS_TOKEN_COUNTER_BACKEND", "huggingface")
        os.environ.setdefault("AS_TOKEN_COUNTER_MODEL_NAME", "gpt2")
        
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

        self._setup_summary_toolkit()

    def _setup_summary_toolkit(self) -> None:
        """Register file tools for use during summarization."""
        try:
            from agentscope.tool import Toolkit
            from markbot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool

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
            logger.warning("Failed to setup summary toolkit: {}", e)
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
                f"reme-ai version mismatch, expected={_EXPECTED_REME_VERSION}. "
                f"Run `pip install reme-ai=={_EXPECTED_REME_VERSION}` to align."
            )

    def _prepare_model_formatter(self) -> None:
        pass

    async def start(self):
        self._warn_if_version_mismatch()
        if self._reme is None:
            logger.warning("Cannot start memory manager: _reme is None")
            return None
        try:
            result = await self._reme.start()
            self._started = True
            logger.info(f"Memory manager started successfully, working_dir={self.working_dir}")
            memory_dir = Path(self.working_dir) / "memory"
            logger.info(f"Memory directory: {memory_dir}, exists={memory_dir.exists()}")
            return result
        except Exception as e:
            logger.error(f"Failed to start memory manager: {e}")
            return None

    async def close(self) -> bool:
        self._warn_if_version_mismatch()
        logger.info(f"MemoryManager closing: working_dir={self.working_dir}")
        if self._reme is None:
            return True
        result = await self._reme.close()
        self._started = False
        logger.info(f"MemoryManager closed: result={result}")
        return result

    async def compact_tool_result(self, **kwargs):
        self._warn_if_version_mismatch()
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
        self._warn_if_version_mismatch()
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
        return await self._reme.check_context(**kwargs)

    async def compact_memory(
        self,
        messages: list,
        previous_summary: str = "",
        extra_instruction: str = "",
        **kwargs,
    ) -> str:
        self._warn_if_version_mismatch()
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
            filepath = os.path.join(self.working_dir, f"compact_invalid_{unique_id}.json")
            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                logger.error(
                    f"Invalid compact result saved to {filepath}. "
                    f"user_msg: {result.get('user_message', '')[:200]}..., "
                    f"history_compact: {result.get('history_compact', '')[:200]}..."
                )
            except Exception:
                logger.error(f"Failed to save invalid compact result")
            return ""

        return result.get("history_compact", "")

    async def summary_memory(self, messages: list, **kwargs) -> str:
        self._warn_if_version_mismatch()
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
    ) -> list[dict]:
        self._warn_if_version_mismatch()
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
                            results.append({"content": block.text})
                        elif isinstance(block, dict) and "text" in block:
                            results.append({"content": block["text"]})
                elif isinstance(result, str):
                    results.append({"content": result})
                elif isinstance(result, list):
                    results.extend({"content": str(r)} for r in result)
                else:
                    results.append({"content": str(result)})
            except Exception as e:
                logger.error(f"Memory search failed: {e}")

        if not results:
            results = self._search_daily_logs(query, max_results=max_results)

        return results

    def _search_daily_logs(
        self,
        query: str,
        max_results: int = 5,
    ) -> list[dict]:
        """Keyword search over workspace/memory/daily/*.md files.

        Uses simple token matching (split query into words, count hits
        per file section).  This is a lightweight fallback — ReMeLight
        does not index the ``memory/daily/`` subdirectory.
        """
        daily_dir = Path(self.working_dir) / "memory" / "daily"
        if not daily_dir.is_dir():
            return []

        query_tokens = set(re.findall(r"\w+", query.lower()))
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
                section_text = section.lower()
                hits = sum(1 for t in query_tokens if t in section_text)
                if hits == 0:
                    continue
                score = hits / len(query_tokens)
                header = section.split("\n", 1)[0][:80]
                content = section.strip()
                if len(content) > 2000:
                    content = content[:2000] + "\n... [truncated]"
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
        self._warn_if_version_mismatch()
        if self._reme is None:
            return None
        return self._reme.get_in_memory_memory()

    def get_compressed_summary(self) -> str:
        return self._compressed_summary

    def set_compressed_summary(self, summary: str) -> None:
        self._compressed_summary = summary
        self._save_compressed_summary(summary)

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

    def get_memory_context(self, query: str | None = None) -> str:
        parts = []
        memory_md = Path(self.working_dir) / "memory" / "MEMORY.md"
        if memory_md.exists():
            try:
                content = memory_md.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(f"## MEMORY.md\n\n{content}")
            except Exception:
                pass

        if self._compressed_summary:
            parts.append(f"## Compressed Summary\n\n{self._compressed_summary}")

        return "\n\n".join(parts) if parts else ""

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

    async def restart_embedding_model(self):
        self._warn_if_version_mismatch()
        if self._reme is None:
            return
        await self._reme.restart(
            restart_config={
                "embedding_models": {"default": self._build_embedding_config()},
            },
        )
