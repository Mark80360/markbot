from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import JSONResponse

from markbot.web.auth import get_token

router = APIRouter()


@router.get("/api/status")
async def get_status():
    try:
        from markbot import __version__
        version = __version__
    except Exception:
        version = "unknown"
    return JSONResponse({
        "version": version,
        "token": get_token(),
    })
