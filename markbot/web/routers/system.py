from __future__ import annotations

import os
import platform
import time

from fastapi import APIRouter
from starlette.responses import JSONResponse

router = APIRouter()


@router.get("/api/system/stats")
async def system_stats():
    from markbot import __version__
    import psutil
    return JSONResponse({
        "version": __version__,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory": {
            "total": psutil.virtual_memory().total,
            "available": psutil.virtual_memory().available,
            "percent": psutil.virtual_memory().percent,
        },
        "disk": {
            "total": psutil.disk_usage("/").total,
            "used": psutil.disk_usage("/").used,
            "free": psutil.disk_usage("/").free,
            "percent": psutil.disk_usage("/").percent,
        },
        "uptime": time.time() - psutil.boot_time(),
    })
