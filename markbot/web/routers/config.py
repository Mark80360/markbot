from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from starlette.responses import JSONResponse
from pydantic import BaseModel

from markbot.config.loader import load_config, save_config
from markbot.config.schema import Config

router = APIRouter()


class ConfigUpdate(BaseModel):
    config: dict[str, Any] | str


@router.get("/api/config")
async def get_config():
    cfg = load_config()
    return JSONResponse(cfg.model_dump(mode="json", by_alias=True))


@router.get("/api/config/raw")
async def get_raw_config():
    from markbot.config.loader import get_config_path
    p = get_config_path()
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
    cfg = Config.model_validate(parsed)
    save_config(cfg)
    return JSONResponse({"ok": True})
