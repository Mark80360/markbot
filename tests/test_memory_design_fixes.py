"""Regression tests for MEMORY.md design fixes (2026-07).

Covers:
- MemoryStore entry parser accepts template / sectioned / entry-list formats
- Main-session gate for always-on MEMORY.md injection
- No dual injection of curated MEMORY via get_memory_context
- Auto-summary does not pollute curated MEMORY.md by default
- Session-scoped vector search still recalls global curated memories
- Snapshot refreshes after memory_save
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_manager(tmp_path: Path, *, long_term_enabled: bool = False):
    from markbot.memory.manager import MemoryManager

    mgr = MemoryManager(
        working_dir=str(tmp_path),
        long_term_enabled=long_term_enabled,
        memory_summary_enabled=True,
    )
    _run(mgr.start())
    return mgr


class TestMemoryStoreParser:
    def test_parses_entry_list_format(self, tmp_path: Path):
        from markbot.memory.tool import MemoryStore

        p = tmp_path / "MEMORY.md"
        p.write_text(
            "# Agent Memory\n\nprefer dark mode\n\n---\n\nuse postgres\n",
            encoding="utf-8",
        )
        store = MemoryStore(working_dir=tmp_path)
        entries = store.read("memory")["entries"]
        assert any("dark mode" in e for e in entries)
        assert any("postgres" in e for e in entries)

    def test_parses_template_sectioned_markdown(self):
        from markbot.memory.tool import MemoryStore

        text = Path("markbot/templates/MEMORY.md").read_text(encoding="utf-8")
        entries = MemoryStore.parse_entries_text(text)
        assert len(entries) >= 2
        assert any("memory_search" in e for e in entries)

    def test_entry_count_limit(self, tmp_path: Path):
        from markbot.memory.tool import MemoryStore
        from markbot.utils.constants import MAX_MEMORY_ENTRIES

        store = MemoryStore(working_dir=tmp_path, memory_char_limit=10_000_000)
        for i in range(MAX_MEMORY_ENTRIES):
            ok = store.add("memory", f"entry-{i}")
            assert ok["success"], ok
        blocked = store.add("memory", "one-too-many")
        assert blocked["success"] is False
        assert "Entry count limit" in blocked["message"]


class TestMainSessionGate:
    def test_helper_classifies_channels(self):
        from markbot.utils.constants import is_main_memory_session

        assert is_main_memory_session("cli") is True
        assert is_main_memory_session("web") is True
        assert is_main_memory_session(None) is True
        assert is_main_memory_session("dingtalk") is False
        assert is_main_memory_session("feishu") is False
        assert is_main_memory_session("email") is False
        assert is_main_memory_session("qq") is False

    def test_context_builder_skips_memory_on_shared_channel(self, tmp_path: Path):
        from markbot.agent.context import ContextBuilder

        (tmp_path / "MEMORY.md").write_text(
            "# Agent Memory\n\nsecret preference: favorite color is blue\n",
            encoding="utf-8",
        )
        (tmp_path / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
        (tmp_path / "PROFILE.md").write_text(
            "# profile\n\nuser real name is Alice Secret\n",
            encoding="utf-8",
        )
        (tmp_path / "SOUL.md").write_text("# soul\n", encoding="utf-8")

        builder = ContextBuilder(workspace=tmp_path)
        main_prompt = builder.build_system_prompt(channel="cli")
        shared_prompt = builder.build_system_prompt(channel="dingtalk")
        assert "favorite color is blue" in main_prompt
        assert "Alice Secret" in main_prompt
        assert "favorite color is blue" not in shared_prompt
        assert "Alice Secret" not in shared_prompt
        # Workspace operating guide remains available.
        assert "agents" in shared_prompt.lower() or "AGENTS.md" in shared_prompt


class TestNoDualCuratedInjection:
    def test_get_memory_context_excludes_curated_by_default(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, long_term_enabled=False)
        assert mgr._memory_store is not None
        mgr._memory_store.add("memory", "I prefer tabs over spaces")
        mgr.set_compressed_summary("working on auth refactor")

        ctx = mgr.get_memory_context(session_key="cli:1")
        assert "auth refactor" in ctx
        assert "tabs over spaces" not in ctx

    def test_include_curated_opt_in_for_main_session(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, long_term_enabled=False)
        assert mgr._memory_store is not None
        mgr._memory_store.add("memory", "I prefer tabs over spaces")
        ctx = mgr.get_memory_context(
            session_key="cli:1",
            channel="cli",
            include_curated=True,
        )
        assert "tabs over spaces" in ctx

    def test_include_curated_blocked_for_shared_channel(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, long_term_enabled=False)
        assert mgr._memory_store is not None
        mgr._memory_store.add("memory", "private home address 1 Infinite Loop")
        ctx = mgr.get_memory_context(
            session_key="dingtalk:group1",
            channel="dingtalk",
            include_curated=True,
        )
        assert "Infinite Loop" not in ctx


class TestAutoSummaryPolicy:
    def test_summary_does_not_write_memory_md_by_default(self, tmp_path: Path):
        class _FB:
            async def chat_with_fallback(self, messages):
                class R:
                    content = "User prefers PostgreSQL for the project database."
                return R(), None

        mgr = _make_manager(tmp_path, long_term_enabled=False)
        mgr._fallback_manager = _FB()
        mgr.auto_summary_to_curated = False
        before = list(mgr._memory_store.memory_entries) if mgr._memory_store else []
        _run(mgr.summary_memory(messages=[{"role": "user", "content": "remember db?"}]))
        after = list(mgr._memory_store.memory_entries) if mgr._memory_store else []
        assert after == before


class TestSnapshotRefresh:
    def test_add_refreshes_snapshot(self, tmp_path: Path):
        from markbot.memory.tool import MemoryStore

        store = MemoryStore(working_dir=tmp_path)
        assert "fresh fact" not in store.system_prompt_snapshot
        store.add("memory", "fresh fact about deployment host")
        assert "fresh fact about deployment host" in store.system_prompt_snapshot


class TestGlobalCuratedRecall:
    def test_session_scoped_search_keeps_global_memory(self, tmp_path: Path):
        from markbot.memory.embedder import HashingEmbedder
        from markbot.memory.longterm import LongTermMemory
        from markbot.memory.vectorstore import InMemoryVectorStore

        store = InMemoryVectorStore()
        emb = HashingEmbedder()
        ltm = LongTermMemory(tmp_path, embedder=emb, vectorstore=store)

        ltm.index(
            "User prefers dark mode in all editors",
            "memory/memory",
            {},
        )
        ltm.index(
            "today we discussed redis caching for session tokens",
            "turn/user",
            {"channel": "cli", "chat_id": "s1"},
        )

        results = ltm.search(
            "editor theme preference",
            max_results=5,
            min_score=0.01,
            channel="cli",
            chat_id="s1",
        )
        contents = " ".join(r.get("content", "") for r in results)
        assert "dark mode" in contents


class TestSharedChannelPrivacy:
    def test_global_merge_excludes_other_session_turns(self, tmp_path: Path):
        from markbot.memory.embedder import HashingEmbedder
        from markbot.memory.longterm import LongTermMemory
        from markbot.memory.vectorstore import InMemoryVectorStore

        store = InMemoryVectorStore()
        emb = HashingEmbedder()
        ltm = LongTermMemory(tmp_path, embedder=emb, vectorstore=store)

        ltm.index("User prefers dark mode in all editors", "memory/memory", {})
        ltm.index(
            "private home address is 1 Infinite Loop",
            "turn/user",
            {"channel": "cli", "chat_id": "home"},
        )

        results = ltm.search(
            "home address preference",
            max_results=5,
            min_score=0.0,
            channel="dingtalk",
            chat_id="group1",
        )
        contents = " ".join(r.get("content", "") for r in results)
        # curated memory may appear; other session turn must not
        assert "Infinite Loop" not in contents

    def test_system_prompt_block_does_not_reinject_memory(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, long_term_enabled=False)
        assert mgr._memory_store is not None
        mgr._memory_store.add("memory", "secret preference xyz")
        assert mgr.system_prompt_block() == ""


class TestSharedSearchPrivacy:
    def test_memory_search_excludes_curated_on_shared_channel(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, long_term_enabled=False)
        assert mgr._memory_store is not None
        mgr._memory_store.add("memory", "favorite color is blue")
        mgr._memory_store.add("user", "user real name is Alice Secret")
        if mgr._daily_log is not None:
            mgr._daily_log.append_turn(
                user_content="today we discussed redis caching",
                assistant_content="ok",
                channel="dingtalk",
                chat_id="group1",
            )

        shared = _run(mgr.memory_search(
            "favorite color Alice redis",
            max_results=10,
            min_score=0.01,
            channel="dingtalk",
            chat_id="group1",
        ))
        shared_text = " ".join(r.get("content", "") for r in shared)
        assert "favorite color is blue" not in shared_text
        assert "Alice Secret" not in shared_text
        assert "redis" in shared_text.lower()

        main = _run(mgr.memory_search(
            "favorite color Alice",
            max_results=10,
            min_score=0.01,
            channel="cli",
            chat_id="me",
        ))
        main_text = " ".join(r.get("content", "") for r in main)
        assert "favorite color is blue" in main_text
        assert "Alice Secret" in main_text


class TestUnknownChannelFailClosed:
    def test_unrecognized_channel_is_not_main(self):
        from markbot.utils.constants import is_main_memory_session

        assert is_main_memory_session("cli") is True
        assert is_main_memory_session(None) is True
        assert is_main_memory_session("dingtalk") is False
        # Fail closed: unknown providers must not inherit curated always-on access.
        assert is_main_memory_session("mysterychat") is False
        assert is_main_memory_session("custom-im") is False


class TestSharedListAndExplorerPrivacy:
    def test_list_memories_blocked_on_shared_channel(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, long_term_enabled=False)
        assert mgr._memory_store is not None
        mgr._memory_store.add("memory", "favorite color is blue")

        shared = _run(mgr.list_memories(limit=10, channel="dingtalk"))
        main = _run(mgr.list_memories(limit=10, channel="cli"))
        assert shared == []
        assert any("favorite color is blue" in m.get("content", "") for m in main)

    def test_memory_list_tool_blocked_on_shared_channel(self, tmp_path: Path):
        from markbot.tools.memory_tools import MemoryListTool

        mgr = _make_manager(tmp_path, long_term_enabled=False)
        assert mgr._memory_store is not None
        mgr._memory_store.add("memory", "favorite color is blue")
        tool = MemoryListTool(memory_manager=mgr)
        tool.set_session_context("dingtalk", "group1")
        out = _run(tool._legacy_execute(limit=10, target="memory"))
        assert "only available in main/private sessions" in out
        assert "favorite color is blue" not in out

    def test_context_explorer_hides_curated_on_shared(self, tmp_path: Path):
        from markbot.tools.context_explorer import (
            ExploreContextCatalogTool,
            LoadContextTool,
            SearchContextTool,
        )

        (tmp_path / "MEMORY.md").write_text(
            "# Agent Memory\n\nsecret preference: favorite color is blue\n",
            encoding="utf-8",
        )
        (tmp_path / "PROFILE.md").write_text(
            "# profile\n\nuser real name is Alice Secret\n",
            encoding="utf-8",
        )
        (tmp_path / "AGENTS.md").write_text("# agents\n", encoding="utf-8")

        mgr = _make_manager(tmp_path, long_term_enabled=False)
        assert mgr._memory_store is not None
        mgr._memory_store.add("memory", "favorite color is blue")

        catalog = ExploreContextCatalogTool(workspace=tmp_path, memory_manager=mgr)
        catalog.set_session_context("dingtalk", "group1")
        cat_out = _run(catalog._legacy_execute(source_type="all"))
        assert "favorite color is blue" not in cat_out
        assert "Alice Secret" not in cat_out
        # Denial copy may mention MEMORY.md by name; ensure no curated entries are listed.
        assert "### MEMORY.md" not in cat_out
        assert "### PROFILE.md" not in cat_out
        assert "unavailable on shared channels" in cat_out

        search = SearchContextTool(workspace=tmp_path, memory_manager=mgr)
        search.set_session_context("dingtalk", "group1")
        search_out = _run(search._legacy_execute(query="favorite color Alice", source="all"))
        assert "favorite color is blue" not in search_out
        assert "Alice Secret" not in search_out

        load = LoadContextTool(workspace=tmp_path, memory_manager=mgr)
        load.set_session_context("dingtalk", "group1")
        load_out = _run(load._legacy_execute(context_id="mem_0"))
        assert "favorite color is blue" not in load_out
        assert "unavailable on shared channels" in load_out.lower() or "Curated MEMORY.md" in load_out

    def test_prefetch_parses_bare_channel_session_id(self, tmp_path: Path):
        mgr = _make_manager(tmp_path, long_term_enabled=False)
        assert mgr._memory_store is not None
        mgr._memory_store.add("memory", "private home address is 1 Infinite Loop")
        # Bare channel name without chat_id must still fail closed.
        out = mgr.prefetch("home address", session_id="dingtalk")
        assert "Infinite Loop" not in out

