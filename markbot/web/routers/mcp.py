from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse

router = APIRouter()


def _get_config():
    from markbot.config.loader import load_config
    return load_config()


def _save_config(cfg):
    from markbot.config.loader import save_config
    save_config(cfg)


def _server_to_dict(name: str, cfg) -> dict[str, Any]:
    """Convert an MCPServerConfig to a JSON-serializable dict."""
    transport_type = cfg.type
    if not transport_type:
        if cfg.command:
            transport_type = "stdio"
        elif cfg.url:
            transport_type = "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
        else:
            transport_type = None

    return {
        "name": name,
        "type": transport_type,
        "command": cfg.command,
        "args": cfg.args or [],
        "env": cfg.env or {},
        "url": cfg.url,
        "headers": cfg.headers or {},
        "tool_timeout": cfg.tool_timeout,
        "enabled_tools": cfg.enabled_tools or ["*"],
        "enabled": getattr(cfg, "enabled", True),
    }


class McpServerAdd(BaseModel):
    name: str
    type: str | None = None
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""
    headers: dict[str, str] = {}
    tool_timeout: int = 30
    enabled_tools: list[str] = ["*"]


class McpServerUpdate(BaseModel):
    type: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    tool_timeout: int | None = None
    enabled_tools: list[str] | None = None


class McpServerToggle(BaseModel):
    enabled: bool


@router.get("/api/mcp/servers")
async def list_mcp_servers():
    try:
        cfg = _get_config()
        servers = []
        for name, mcp_cfg in cfg.tools.mcp_servers.items():
            servers.append(_server_to_dict(name, mcp_cfg))
        return JSONResponse({"servers": servers})
    except Exception as e:
        return JSONResponse({"servers": [], "error": str(e)}, status_code=500)


@router.post("/api/mcp/servers")
async def add_mcp_server(data: McpServerAdd):
    try:
        from markbot.config.schema import MCPServerConfig
        cfg = _get_config()
        if data.name in cfg.tools.mcp_servers:
            return JSONResponse({"error": f"MCP server '{data.name}' already exists"}, status_code=400)

        server_cfg = MCPServerConfig(
            type=data.type,
            command=data.command,
            args=data.args,
            env=data.env,
            url=data.url,
            headers=data.headers,
            tool_timeout=data.tool_timeout,
            enabled_tools=data.enabled_tools,
        )
        cfg.tools.mcp_servers[data.name] = server_cfg
        _save_config(cfg)
        return JSONResponse({"ok": True, "server": _server_to_dict(data.name, server_cfg)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/api/mcp/servers/{name}")
async def update_mcp_server(name: str, data: McpServerUpdate):
    try:
        cfg = _get_config()
        if name not in cfg.tools.mcp_servers:
            return JSONResponse({"error": "MCP server not found"}, status_code=404)

        server = cfg.tools.mcp_servers[name]
        if data.type is not None:
            server.type = data.type
        if data.command is not None:
            server.command = data.command
        if data.args is not None:
            server.args = data.args
        if data.env is not None:
            server.env = data.env
        if data.url is not None:
            server.url = data.url
        if data.headers is not None:
            server.headers = data.headers
        if data.tool_timeout is not None:
            server.tool_timeout = data.tool_timeout
        if data.enabled_tools is not None:
            server.enabled_tools = data.enabled_tools

        _save_config(cfg)
        return JSONResponse({"ok": True, "server": _server_to_dict(name, server)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/api/mcp/servers/{name}")
async def remove_mcp_server(name: str):
    try:
        cfg = _get_config()
        if name in cfg.tools.mcp_servers:
            del cfg.tools.mcp_servers[name]
            _save_config(cfg)
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": "MCP server not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/mcp/servers/{name}/test")
async def test_mcp_server(name: str):
    """Test MCP server connectivity by attempting a real connection."""
    try:
        cfg = _get_config()
        if name not in cfg.tools.mcp_servers:
            return JSONResponse({"ok": False, "error": "MCP server not found"}, status_code=404)

        mcp_cfg = cfg.tools.mcp_servers[name]
        transport_type = mcp_cfg.type
        if not transport_type:
            if mcp_cfg.command:
                transport_type = "stdio"
            elif mcp_cfg.url:
                transport_type = "sse" if mcp_cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
            else:
                return JSONResponse({"ok": False, "error": "No command or url configured"})

        result = await _test_mcp_connection(transport_type, mcp_cfg)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def _test_mcp_connection(transport_type: str, mcp_cfg) -> dict[str, Any]:
    """Attempt to connect to an MCP server and list its tools."""
    from contextlib import AsyncExitStack
    from markbot.tools.mcp import connect_mcp_servers
    from markbot.tools.registry import ToolRegistry

    # Build a temporary dict in the format connect_mcp_servers expects
    servers_dict = {"_test": mcp_cfg}
    registry = ToolRegistry()
    stack = AsyncExitStack()
    try:
        await asyncio.wait_for(
            _connect_with_timeout(servers_dict, registry, stack),
            timeout=15.0,
        )
        tool_names = [t for t in registry.tool_names if t.startswith("mcp__test_")]
        return {
            "ok": True,
            "message": f"Connected successfully, {len(tool_names)} tools available",
            "tools": tool_names,
        }
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Connection timed out (15s)"}
    except Exception as e:
        return {"ok": False, "error": f"Connection failed: {e}"}
    finally:
        try:
            await stack.aclose()
        except Exception:
            pass


async def _connect_with_timeout(servers_dict, registry, stack):
    from markbot.tools.mcp import connect_mcp_servers
    await connect_mcp_servers(servers_dict, registry, stack)


@router.put("/api/mcp/servers/{name}/enabled")
async def toggle_mcp_server(name: str, data: McpServerToggle):
    """Toggle MCP server enabled state."""
    try:
        cfg = _get_config()
        if name not in cfg.tools.mcp_servers:
            return JSONResponse({"ok": False, "error": "MCP server not found"}, status_code=404)

        cfg.tools.mcp_servers[name].enabled = data.enabled
        _save_config(cfg)
        return JSONResponse({"ok": True, "enabled": data.enabled})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
