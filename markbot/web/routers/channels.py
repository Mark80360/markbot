from __future__ import annotations

import asyncio
import socket
from typing import Any
from urllib.parse import urlparse

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


def _extract_host(section: Any) -> str | None:
    """Extract host from a channel config section."""
    for attr in ("host", "ws_host", "server", "base_url", "url"):
        val = getattr(section, attr, None) if not isinstance(section, dict) else section.get(attr)
        if val and isinstance(val, str):
            parsed = urlparse(val if "://" in val else f"scheme://{val}")
            return parsed.hostname or val.split(":")[0].strip()
    return None


def _extract_port(section: Any) -> int | None:
    """Extract port from a channel config section."""
    for attr in ("port", "ws_port"):
        val = getattr(section, attr, None) if not isinstance(section, dict) else section.get(attr)
        if val:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    return None


def _channel_to_dict(name: str, section: Any) -> dict[str, Any]:
    """Convert a channel config section to a JSON-serializable dict."""
    if isinstance(section, dict):
        data = dict(section)
    elif hasattr(section, "model_dump"):
        # Pydantic model: use model_dump to get only real fields (excludes internals)
        data = section.model_dump(exclude_none=False)
    else:
        data = {}

    enabled = data.get("enabled", False)
    host = _extract_host(section)
    port = _extract_port(section)

    return {
        "id": name,
        "name": name,
        "enabled": enabled,
        "host": host,
        "port": port,
        "status": "enabled" if enabled else "disabled",
        "config": data,
    }


@router.get("/api/channels")
async def list_channels():
    try:
        cfg = _get_config()
        channels = cfg.channels
        result = []
        # ChannelsConfig uses extra="allow", so extra fields are stored in model_extra
        extra = getattr(channels, "model_extra", None) or {}
        # Also check model_fields_set for dynamically added fields
        all_fields = {}
        # Get defined fields first
        for field_name in type(channels).model_fields:
            all_fields[field_name] = getattr(channels, field_name, None)
        # Then add extra (channel-specific) fields
        all_fields.update(extra)

        for name, settings in all_fields.items():
            if name in ("send_progress", "send_tool_hints", "send_max_retries"):
                continue
            if isinstance(settings, dict):
                result.append(_channel_to_dict(name, settings))
            elif hasattr(settings, "model_dump"):
                result.append(_channel_to_dict(name, settings.model_dump()))
            else:
                result.append(_channel_to_dict(name, settings))

        return JSONResponse({"channels": result})
    except Exception as e:
        return JSONResponse({"channels": [], "error": str(e)}, status_code=500)


@router.post("/api/channels/{channel_id}/test")
async def test_channel(channel_id: str):
    """Test channel connectivity by attempting a TCP socket connection."""
    try:
        cfg = _get_config()
        channels = cfg.channels
        extra = getattr(channels, "model_extra", None) or {}
        section = extra.get(channel_id)

        if section is None:
            # Check if it's a defined field
            if hasattr(channels, channel_id):
                section = getattr(channels, channel_id)
            else:
                return JSONResponse({"ok": False, "error": f"Channel '{channel_id}' not found"}, status_code=404)

        if isinstance(section, dict):
            host = _extract_host(section)
            port = _extract_port(section)
        else:
            host = _extract_host(section)
            port = _extract_port(section)

        if not host:
            return JSONResponse({"ok": False, "error": "No host configured for this channel"})

        if not port:
            # Try common defaults based on channel type
            if channel_id == "slack":
                port = 443
            elif channel_id == "telegram":
                port = 443
            elif channel_id == "discord":
                port = 443
            else:
                return JSONResponse({"ok": False, "error": "No port configured for this channel"})

        # Attempt TCP connection with timeout
        try:
            _, _ = await asyncio.wait_for(
                asyncio.get_event_loop().getaddrinfo(host, port),
                timeout=5.0,
            )
            future = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(future, timeout=5.0)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return JSONResponse({
                "ok": True,
                "message": f"Successfully connected to {host}:{port}",
                "host": host,
                "port": port,
            })
        except asyncio.TimeoutError:
            return JSONResponse({"ok": False, "error": f"Connection to {host}:{port} timed out (5s)"})
        except (ConnectionRefusedError, socket.gaierror, OSError) as e:
            return JSONResponse({"ok": False, "error": f"Connection to {host}:{port} failed: {e}"})

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


class ChannelToggle(BaseModel):
    enabled: bool


@router.put("/api/channels/{channel_id}/enabled")
async def toggle_channel(channel_id: str, data: ChannelToggle):
    """Toggle a channel's enabled state in config."""
    try:
        cfg = _get_config()
        channels = cfg.channels
        extra = getattr(channels, "model_extra", None) or {}

        # Check extra fields first, then defined fields
        if channel_id in extra:
            section = extra[channel_id]
        elif hasattr(channels, channel_id):
            section = getattr(channels, channel_id)
        else:
            return JSONResponse({"ok": False, "error": f"Channel '{channel_id}' not found"}, status_code=404)

        if isinstance(section, dict):
            section["enabled"] = data.enabled
        else:
            section.enabled = data.enabled

        _save_config(cfg)
        return JSONResponse({"ok": True, "enabled": data.enabled})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
