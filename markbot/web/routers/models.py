from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse
from markbot.config.loader import load_config, save_config

router = APIRouter()


def _get_current_model(cfg) -> dict[str, Any]:
    """Extract current provider/model from model_chain."""
    chain = cfg.agents.defaults.model_chain or []
    if not chain:
        return {"provider": "", "model": "", "chain": []}

    first = chain[0]
    parts = first.split("/", 1)
    provider = parts[0] if len(parts) > 0 else ""
    model = parts[1] if len(parts) > 1 else ""
    return {"provider": provider, "model": model, "chain": chain}


@router.get("/api/model/info")
async def model_info():
    cfg = load_config()
    info = _get_current_model(cfg)
    info["max_tokens"] = cfg.agents.defaults.max_tokens
    info["temperature"] = cfg.agents.defaults.temperature
    info["context_window"] = cfg.agents.defaults.context_window_tokens
    info["reasoning_effort"] = cfg.agents.defaults.reasoning_effort
    info["max_tool_iterations"] = cfg.agents.defaults.max_tool_iterations
    return JSONResponse(info)


@router.get("/api/model/options")
async def model_options():
    """List all configured providers and their models."""
    cfg = load_config()
    providers = cfg.providers
    options = []

    # Get all provider IDs using the correct method name
    provider_ids = providers.list_provider_ids() if hasattr(providers, "list_provider_ids") else []
    # Also check named fields for providers that may not be configured yet
    for attr in ("custom", "azure_openai", "anthropic", "openai", "openrouter",
                 "deepseek", "groq", "zhipu", "dashscope"):
        if attr not in provider_ids:
            provider_ids.append(attr)

    for pid in provider_ids:
        provider = providers.get_provider(pid) if hasattr(providers, "get_provider") else None
        if not provider:
            continue

        models = []
        for m in (provider.models or []):
            models.append({
                "id": m.id,
                "name": m.name,
                "max_tokens": m.max_tokens,
                "context_window": m.context_window,
                "capabilities": m.capabilities or [],
                "reasoning_effort": m.reasoning_effort,
            })

        options.append({
            "provider": pid,
            "has_api_key": bool(provider.api_key),
            "api_base": provider.api_base,
            "is_configured": provider.is_configured if hasattr(provider, "is_configured") else False,
            "models": models,
        })

    return JSONResponse({"options": options})


class ModelSet(BaseModel):
    provider: str
    model: str  # model id within provider


@router.post("/api/model/set")
async def set_model(data: ModelSet):
    cfg = load_config()
    ref = f"{data.provider}/{data.model}"
    chain = cfg.agents.defaults.model_chain or []
    # Remove existing entry for this provider/model, then prepend
    chain = [c for c in chain if c != ref]
    chain.insert(0, ref)
    cfg.agents.defaults.model_chain = chain
    save_config(cfg)
    return JSONResponse({"ok": True, "chain": chain})


class AgentParamsUpdate(BaseModel):
    max_tokens: int | None = None
    temperature: float | None = None
    context_window_tokens: int | None = None
    reasoning_effort: str | None = None
    max_tool_iterations: int | None = None


@router.put("/api/model/params")
async def update_agent_params(data: AgentParamsUpdate):
    cfg = load_config()
    if data.max_tokens is not None:
        cfg.agents.defaults.max_tokens = data.max_tokens
    if data.temperature is not None:
        cfg.agents.defaults.temperature = data.temperature
    if data.context_window_tokens is not None:
        cfg.agents.defaults.context_window_tokens = data.context_window_tokens
    if data.reasoning_effort is not None:
        cfg.agents.defaults.reasoning_effort = data.reasoning_effort
    if data.max_tool_iterations is not None:
        cfg.agents.defaults.max_tool_iterations = data.max_tool_iterations
    save_config(cfg)
    return JSONResponse({"ok": True})
