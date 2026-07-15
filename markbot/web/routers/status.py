from __future__ import annotations

import logging

from fastapi import APIRouter
from starlette.responses import JSONResponse

_log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/status")
async def get_status():
    try:
        from markbot import __version__
        version = __version__
    except Exception as e:
        _log.debug("Failed to get version: %s", e)
        version = "unknown"
    return JSONResponse({
        "version": version,
    })
