"""Tests for specific tool implementations — think, question, todo."""


import pytest

from markbot.tools.question import AskUserQuestionTool
from markbot.tools.think import ThinkTool
from markbot.tools.todo import TodoTool, _short_id, _TodoStore


class TestThinkTool:
    @pytest.fixture
    def tool(self):
        return ThinkTool()

    def test_name(self, tool):
        assert tool.name == "think"

    def test_description(self, tool):
        assert "thinking" in tool.description.lower()

    def test_parameters(self, tool):
        params = tool.parameters
        assert "question" in params["properties"]
        assert "mode" in params["properties"]
        assert "question" in params["required"]

    @pytest.mark.asyncio
    async def test_analyze_mode(self, tool):
        result = await tool._legacy_execute(question="What is the best approach?")
        assert "Analysis" in result or "Framework" in result

    @pytest.mark.asyncio
    async def test_challenge_mode(self, tool):
        result = await tool._legacy_execute(
            question="Is this correct?",
            mode="challenge",
        )
        assert "Challenge" in result or "assumptions" in result.lower()

    @pytest.mark.asyncio
    async def test_inversion_mode(self, tool):
        result = await tool._legacy_execute(
            question="How to succeed?",
            mode="inversion",
        )
        assert "Inversion" in result or "fail" in result.lower()

    @pytest.mark.asyncio
    async def test_first_principles_mode(self, tool):
        result = await tool._legacy_execute(
            question="What should we build?",
            mode="first-principles",
        )
        assert "First Principles" in result

    @pytest.mark.asyncio
    async def test_plan_mode(self, tool):
        result = await tool._legacy_execute(
            question="Build a web app",
            mode="plan",
            constraints="2 weeks",
            detail_level="high",
        )
        assert "Plan" in result
        assert "Build a web app" in result

    @pytest.mark.asyncio
    async def test_evaluate_mode(self, tool):
        result = await tool._legacy_execute(
            question="Deploy to production",
            mode="evaluate",
            result="Deployed successfully",
            expected="Zero downtime",
        )
        assert "Evaluate" in result

    @pytest.mark.asyncio
    async def test_code_analysis_mode(self, tool):
        result = await tool._legacy_execute(
            question="How does auth work?",
            mode="code-analysis",
        )
        assert "Code Analysis" in result

    @pytest.mark.asyncio
    async def test_research_plan_mode(self, tool):
        result = await tool._legacy_execute(
            question="Understand the architecture",
            mode="research-plan",
        )
        assert "Research Plan" in result


class TestAskUserQuestionTool:
    @pytest.fixture
    def tool(self):
        return AskUserQuestionTool()

    def test_name(self, tool):
        assert tool.name == "ask_user_question"

    def test_parameters(self, tool):
        params = tool.parameters
        assert "question" in params["properties"]
        assert "options" in params["properties"]

    def test_format_question(self, tool):
        question = "What framework?"
        options = [
            {"label": "React", "description": "Facebook's UI library"},
            {"label": "Vue", "description": "Progressive framework"},
        ]
        result = tool._format_question(question, options)
        assert "React" in result
        assert "Vue" in result
        assert "What framework?" in result

    def test_format_question_no_description(self, tool):
        question = "Choose one"
        options = [{"label": "A"}, {"label": "B"}]
        result = tool._format_question(question, options)
        assert "1. A" in result
        assert "2. B" in result

    def test_set_context(self, tool):
        tool.set_context("dingtalk", "chat123")
        assert tool._default_channel == "dingtalk"
        assert tool._default_chat_id == "chat123"

    async def test_handle_response(self, tool):
        # Must be async so we get a running event loop. ``asyncio.Future()``
        # without a running loop works for ``.set_result()`` on most
        # versions but ``.result()``/``await`` requires one, and in
        # Python 3.12+ the deprecation path can raise RuntimeError when
        # the test runs after another test has closed the implicit loop.
        # pytest-asyncio is in auto mode (see pyproject.toml), so the
        # ``async`` keyword is enough — no decorator required.
        import asyncio
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        tool._pending_questions["q1"] = future
        tool.handle_response("q1", "React")
        # ``handle_response`` calls ``set_result`` synchronously, so the
        # future is already done by the time we get here; ``await`` is
        # the only safe way to read it back without ``.result()`` blocking.
        assert future.done()
        assert future.result() == "React"

    def test_handle_response_unknown_id(self, tool):
        # Should not raise
        tool.handle_response("unknown", "test")


class TestTodoStore:
    @pytest.fixture
    def store(self, tmp_path):
        return _TodoStore(tmp_path)

    def test_create_item(self, store):
        results = store.write([{"content": "Test task"}])
        assert len(results) == 1
        assert results[0]["content"] == "Test task"
        assert results[0]["status"] == "pending"
        assert results[0]["id"] != ""

    def test_create_multiple(self, store):
        results = store.write([
            {"content": "Task 1"},
            {"content": "Task 2"},
        ])
        assert len(results) == 2

    def test_update_item(self, store):
        results = store.write([{"content": "Original"}])
        item_id = results[0]["id"]
        updated = store.write([{"id": item_id, "content": "Updated", "status": "completed"}])
        assert updated[0]["content"] == "Updated"
        assert updated[0]["status"] == "completed"

    def test_update_nonexistent(self, store):
        results = store.write([{"id": "nonexistent", "content": "test"}])
        assert "error" in results[0]

    def test_list_items(self, store):
        store.write([{"content": "A", "status": "pending"}, {"content": "B", "status": "completed"}])
        all_items = store.list_items()
        assert len(all_items) == 2

    def test_list_filter_status(self, store):
        store.write([{"content": "A", "status": "pending"}, {"content": "B", "status": "completed"}])
        pending = store.list_items(status="pending")
        assert len(pending) == 1
        assert pending[0]["content"] == "A"

    def test_list_filter_priority(self, store):
        store.write([{"content": "A", "priority": "high"}, {"content": "B", "priority": "low"}])
        high = store.list_items(priority="high")
        assert len(high) == 1

    def test_delete_items(self, store):
        results = store.write([{"content": "To delete"}])
        item_id = results[0]["id"]
        removed = store.delete([item_id])
        assert len(removed) == 1
        assert store.list_items() == []

    def test_delete_nonexistent(self, store):
        removed = store.delete(["nonexistent"])
        assert len(removed) == 0

    def test_persistence(self, tmp_path):
        store1 = _TodoStore(tmp_path)
        store1.write([{"content": "Persistent"}])
        store2 = _TodoStore(tmp_path)
        items = store2.list_items()
        assert len(items) == 1
        assert items[0]["content"] == "Persistent"


class TestTodoTool:
    @pytest.fixture
    def tool(self, tmp_path):
        return TodoTool(workspace=tmp_path)

    def test_name(self, tool):
        assert tool.name == "todo"

    def test_is_read_only_list(self, tool):
        assert tool.is_read_only({"action": "list"}) is True

    def test_is_read_only_write(self, tool):
        assert tool.is_read_only({"action": "write"}) is False

    @pytest.mark.asyncio
    async def test_write_action(self, tool):
        result = await tool._legacy_execute(
            action="write",
            items=[{"content": "Test task"}],
        )
        assert "Test task" in result

    @pytest.mark.asyncio
    async def test_write_no_items(self, tool):
        result = await tool._legacy_execute(action="write", items=[])
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_list_action(self, tool):
        await tool._legacy_execute(action="write", items=[{"content": "Task 1"}])
        result = await tool._legacy_execute(action="list")
        assert "Task 1" in result

    @pytest.mark.asyncio
    async def test_list_empty(self, tool):
        result = await tool._legacy_execute(action="list")
        assert "No todo items" in result

    @pytest.mark.asyncio
    async def test_delete_action(self, tool):
        write_result = await tool._legacy_execute(
            action="write",
            items=[{"content": "To delete"}],
        )
        # Extract ID from result
        item_id = write_result.split("[")[1].split("]")[0]
        result = await tool._legacy_execute(action="delete", ids=[item_id])
        assert "Deleted" in result

    @pytest.mark.asyncio
    async def test_delete_no_ids(self, tool):
        result = await tool._legacy_execute(action="delete", ids=[])
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_unknown_action(self, tool):
        result = await tool._legacy_execute(action="unknown")
        assert "Error" in result

    def test_set_session(self, tool):
        tool.set_session("test_session")
        assert tool._session_id == "test_session"


class TestShortId:
    def test_returns_string(self):
        result = _short_id()
        assert isinstance(result, str)
        assert len(result) == 8

    def test_avoids_existing(self):
        existing = {"a" * 8, "b" * 8}
        for _ in range(10):
            result = _short_id(existing)
            assert result not in existing
