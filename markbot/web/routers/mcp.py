from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse

router = APIRouter()

_mcp_servers: dict[str, dict[str, Any]] = {}


class McpServerAdd(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict[str, str] = {}


class McpServerToggle(BaseModel):
    enabled: bool


@router.get("/api/mcp/servers")
async def list_mcp_servers():
    return JSONResponse({"servers": list(_mcp_servers.values())})


@router.post("/api/mcp/servers")
async def add_mcp_server(data: McpServerAdd):
    _mcp_servers[data.name] = {
        "name": data.name,
        "command": data.command,
        "args": data.args,
        "env": data.env,
        "enabled": True,
        "status": "configured",
    }
    return JSONResponse({"ok": True})


@router.delete("/api/mcp/servers/{name}")
async def remove_mcp_server(name: str):
    _mcp_servers.pop(name, None)
    return JSONResponse({"ok": True})


@router.post("/api/mcp/servers/{name}/test")
async def test_mcp_server(name: str):
    return JSONResponse({"ok": True, "message": f"MCP server '{name}' test initiated"})


@router.put("/api/mcp/servers/{name}/enabled")
async def toggle_mcp_server(name: str, data: McpServerToggle):
    server = _mcp_servers.get(name)
    if server:
        server["enabled"] = data.enabled
    return JSONResponse({"ok": True})
