"""Tests for built-in slash commands."""

from __future__ import annotations

import json
from types import SimpleNamespace


class _FakeAppState:
    """Minimal stand-in for AppStateProvider."""

    def __init__(self):
        from markbot.types.permission import PermissionMode, ToolPermissionContext
        self._mode = PermissionMode.DEFAULT
        self._tool_ctx = ToolPermissionContext(mode=PermissionMode.DEFAULT)

    def set_permission_mode(self, mode):
        self._mode = mode
        from markbot.types.permission import ToolPermissionContext
        self._tool_ctx = ToolPermissionContext(mode=mode)

    def get_permission_mode(self):
        return self._mode


class _FakeLoop:
    def __init__(self):
        self.ctx = SimpleNamespace(app_state=_FakeAppState())


def _make_ctx(args: str, loop=None):
    from markbot.bus.events import InboundMessage
    from markbot.cli.slash_commands.router import CommandContext
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/mode " + args)
    return CommandContext(msg=msg, session=None, key="cli:direct", raw="/mode " + args, args=args, loop=loop or _FakeLoop())


class TestCmdModePersist:
    """``/mode`` must persist the chosen mode to config.json so it survives
    restarts, not just flip the in-memory app_state.

    Regression for logs/2026-07-05.log: ``/mode auto`` set in the evening
    was lost after gateway restart.
    """

    def test_mode_auto_persists_to_config(self, tmp_path, monkeypatch):
        from markbot.config import loader as loader_mod
        from markbot.cli.slash_commands.builtin import cmd_mode

        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        # Isolate from the real ~/.markbot/config.json
        monkeypatch.setattr(loader_mod, "get_config_path", lambda: cfg)

        loop = _FakeLoop()
        ctx = _make_ctx("auto", loop=loop)

        import asyncio
        result = asyncio.run(cmd_mode(ctx))

        assert result is not None
        assert "auto" in result.content
        # In-memory switch took effect
        from markbot.types.permission import PermissionMode
        assert loop.ctx.app_state.get_permission_mode() is PermissionMode.AUTO
        # Persisted to file (camelCase key matches schema alias_generator=to_camel)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["agents"]["defaults"]["defaultPermissionMode"] == "auto"

    def test_mode_default_persists_to_config(self, tmp_path, monkeypatch):
        from markbot.config import loader as loader_mod
        from markbot.cli.slash_commands.builtin import cmd_mode

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"agents": {"defaults": {"defaultPermissionMode": "auto"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(loader_mod, "get_config_path", lambda: cfg)

        loop = _FakeLoop()
        ctx = _make_ctx("default", loop=loop)

        import asyncio
        asyncio.run(cmd_mode(ctx))

        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["agents"]["defaults"]["defaultPermissionMode"] == "default"

    def test_mode_switch_still_succeeds_when_persist_fails(self, tmp_path, monkeypatch):
        # Point get_config_path at a path whose parent cannot be created
        # (a file blocking the dir) so the write fails. The in-memory
        # switch must still take effect.
        from markbot.config import loader as loader_mod
        from markbot.cli.slash_commands.builtin import cmd_mode

        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        cfg = blocker / "config.json"  # parent is a file, mkdir will fail
        monkeypatch.setattr(loader_mod, "get_config_path", lambda: cfg)

        loop = _FakeLoop()
        ctx = _make_ctx("auto", loop=loop)

        import asyncio
        result = asyncio.run(cmd_mode(ctx))

        # In-memory switch succeeded despite persist failure
        from markbot.types.permission import PermissionMode
        assert loop.ctx.app_state.get_permission_mode() is PermissionMode.AUTO
        # User sees a warning in the response
        assert "warning" in result.content.lower()

    def teardown_method(self):
        from markbot.config import loader as loader_mod
        loader_mod._current_config = None
        loader_mod._current_config_path = None
