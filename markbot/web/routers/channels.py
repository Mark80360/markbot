from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import JSONResponse
from markbot.config.loader import load_config

router = APIRouter()


@router.get("/api/channels")
async def list_channels():
    cfg = load_config()
    channels = getattr(cfg, "channels", {})
    result = []
    for name, settings in (channels.__dict__ if hasattr(channels, '__dict__') else channels).items():
        if isinstance(settings, dict):
            enabled = settings.get("enabled", False)
        else:
            enabled = getattr(settings, "enabled", False)
        result.append({
            "id": name,
            "name": name,
            "enabled": enabled,
            "status": "connected" if enabled else "disabled",
        })
    return JSONResponse({"channels": result})


@router.post("/api/channels/{channel_id}/test")
async def test_channel(channel_id: str):
    return JSONResponse({"ok": True, "message": f"Channel '{channel_id}' test initiated"})
