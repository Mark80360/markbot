from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from starlette.responses import JSONResponse

router = APIRouter()

_log = logging.getLogger(__name__)

DEFAULT_LOG_DIR = Path.home() / ".markbot" / "logs"


@router.get("/api/logs")
async def get_logs(file: str = "markbot.log", lines: int = 200, level: str = "", component: str = ""):
    log_dir = DEFAULT_LOG_DIR
    if not log_dir.exists():
        return JSONResponse({"logs": [], "file": file, "path": str(log_dir)})
    log_file = log_dir / file
    if not log_file.exists():
        return JSONResponse({"logs": [], "file": file, "path": str(log_file)})
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
        entries = text.strip().split("\n")[-lines:]
        if level:
            entries = [e for e in entries if level.upper() in e]
        if component:
            entries = [e for e in entries if component.lower() in e.lower()]
        return JSONResponse({"logs": entries, "file": file, "path": str(log_file)})
    except Exception as e:
        _log.exception("Failed to read log file")
        return JSONResponse({"logs": [], "file": file, "error": str(e)})


@router.get("/api/logs/files")
async def list_log_files():
    log_dir = DEFAULT_LOG_DIR
    if not log_dir.exists():
        return JSONResponse({"files": []})
    files = sorted(f.name for f in log_dir.iterdir() if f.is_file() and f.suffix in (".log", ".txt"))
    return JSONResponse({"files": files})
