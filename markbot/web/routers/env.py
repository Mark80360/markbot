from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse

router = APIRouter()

SECRET_KEYS = {"API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PASSWORD", "SECRET", "TOKEN"}


def _list_env() -> list[dict[str, Any]]:
    results = []
    for k, v in sorted(os.environ.items()):
        is_secret = any(secret in k.upper() for secret in SECRET_KEYS)
        results.append({
            "key": k,
            "value": "****" if is_secret and v else v,
            "is_secret": is_secret,
        })
    return results


class EnvSet(BaseModel):
    key: str
    value: str


class EnvReveal(BaseModel):
    key: str


@router.get("/api/env")
async def list_env():
    return JSONResponse({"env": _list_env()})


@router.put("/api/env")
async def set_env(data: EnvSet):
    os.environ[data.key] = data.value
    return JSONResponse({"ok": True})


@router.delete("/api/env")
async def delete_env(key: str):
    os.environ.pop(key, None)
    return JSONResponse({"ok": True})


@router.post("/api/env/reveal")
async def reveal_env(data: EnvReveal):
    value = os.environ.get(data.key, "")
    return JSONResponse({"key": data.key, "value": value})
