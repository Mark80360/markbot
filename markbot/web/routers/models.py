from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse
from markbot.config.loader import load_config, save_config

router = APIRouter()


@router.get("/api/model/info")
async def model_info():
    cfg = load_config()
    provider = getattr(cfg.agents, "provider", "openai")
    model = getattr(cfg.agents, "model", "gpt-4o")
    return JSONResponse({"provider": provider, "model": model})


@router.get("/api/model/options")
async def model_options():
    return JSONResponse({
        "options": [
            {"provider": "openai", "models": ["gpt-4o", "gpt-4o-mini", "o3-mini"]},
            {"provider": "anthropic", "models": ["claude-sonnet-4-20250514", "claude-haiku-3-5"]},
            {"provider": "google", "models": ["gemini-2.5-flash", "gemini-2.5-pro"]},
            {"provider": "deepseek", "models": ["deepseek-chat", "deepseek-reasoner"]},
        ]
    })


class ModelSet(BaseModel):
    provider: str
    model: str


@router.post("/api/model/set")
async def set_model(data: ModelSet):
    cfg = load_config()
    cfg.agents.provider = data.provider
    cfg.agents.model = data.model
    save_config(cfg)
    return JSONResponse({"ok": True})
