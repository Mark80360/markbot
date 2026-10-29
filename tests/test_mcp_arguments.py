"""MCP tool arguments must contain only model-supplied schema fields."""

import sys
import types

import pytest

from markbot.tools.mcp import MCPToolWrapper


@pytest.fixture
def fake_mcp(monkeypatch):
    """Install a minimal ``mcp`` module so the test needs no SDK install."""
    fake = types.ModuleType("mcp")
    fake_types = types.ModuleType("mcp.types")

    class TextContent:
        def __init__(self, text: str):
            self.text = text

    fake_types.TextContent = TextContent
    fake.types = fake_types
    monkeypatch.setitem(sys.modules, "mcp", fake)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types)
    return fake_types


class _ToolDef:
    name = "echo"
    description = "echo back"
    inputSchema = {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
        "required": ["msg"],
    }


class _Session:
    def __init__(self, content_type):
        self.calls: list[tuple[str, dict]] = []
        self._content_type = content_type

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))

        class _Result:
            content = [self._content_type("ok")]

        return _Result()


@pytest.mark.asyncio
async def test_tool_context_is_not_forwarded(fake_mcp):
    session = _Session(fake_mcp.TextContent)
    wrapper = MCPToolWrapper(session, "srv", _ToolDef())

    result = await wrapper._legacy_execute(msg="hi", _tool_context=object())

    assert result == "ok"
    assert session.calls == [("echo", {"msg": "hi"})]


@pytest.mark.asyncio
async def test_business_arguments_are_preserved(fake_mcp):
    session = _Session(fake_mcp.TextContent)
    wrapper = MCPToolWrapper(session, "srv", _ToolDef())

    await wrapper._legacy_execute(msg="hello", _tool_context=None)

    assert session.calls == [("echo", {"msg": "hello"})]
