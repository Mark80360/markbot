from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse
from markbot.web.store import WebSessionStore

router = APIRouter()

store = WebSessionStore()


class SessionRename(BaseModel):
    title: str


@router.get("/api/sessions")
async def list_sessions(limit: int = 50, offset: int = 0):
    sessions = store.list_sessions(limit=limit, offset=offset)
    return JSONResponse({"sessions": sessions})


@router.delete("/api/sessions/empty")
async def delete_empty():
    count = store.delete_empty_sessions()
    return JSONResponse({"ok": True, "deleted": count})


@router.get("/api/sessions/search")
async def search_sessions(q: str = ""):
    results = store.search_sessions(q)
    return JSONResponse({"sessions": results})


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    data = store.get_session(session_id)
    if not data:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse(data)


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    ok = store.delete_session(session_id)
    return JSONResponse({"ok": ok})


@router.patch("/api/sessions/{session_id}")
async def patch_session(session_id: str, data: SessionRename):
    ok = store.update_title(session_id, data.title)
    return JSONResponse({"ok": ok})
