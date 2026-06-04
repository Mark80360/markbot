from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from starlette.responses import JSONResponse
from pydantic import BaseModel

from markbot.config.loader import load_config, save_config

router = APIRouter()


class ConfigUpdate(BaseModel):
    config: dict[str, Any] | str


@router.get("/api/config")
async def get_config():
    cfg = load_config()
    return JSONResponse(cfg.to_dict() if hasattr(cfg, 'to_dict') else cfg.__dict__)


@router.get("/api/config/raw")
async def get_raw_config():
    from pathlib import Path
    from markbot.config.defaults import default_config_path
    p = default_config_path()
    if p and p.exists():
        return JSONResponse({"raw": p.read_text(encoding="utf-8")})
    return JSONResponse({"raw": ""})


@router.put("/api/config")
async def update_config(data: ConfigUpdate):
    if isinstance(data.config, str):
        import yaml
        parsed = yaml.safe_load(data.config)
    else:
        parsed = data.config
    save_config(parsed)
    return JSONResponse({"ok": True})
