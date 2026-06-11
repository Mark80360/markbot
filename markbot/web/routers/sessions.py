from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse
from markbot.web.store import WebSessionStore

router = APIRouter()

store = WebSessionStore()


class SessionRename(BaseModel):
    title: str


class BulkDeleteRequest(BaseModel):
    ids: list[str]


@router.get("/api/sessions")
async def list_sessions(limit: int = 50, offset: int = 0):
    sessions = store.list_sessions(limit=limit, offset=offset)
    return JSONResponse({"sessions": sessions})


@router.get("/api/sessions/stats")
async def session_stats():
    stats = store.get_session_stats()
    return JSONResponse(stats)


@router.delete("/api/sessions/empty")
async def delete_empty():
    count = store.delete_empty_sessions()
    return JSONResponse({"ok": True, "deleted": count})


@router.post("/api/sessions/bulk-delete")
async def bulk_delete(data: BulkDeleteRequest):
    count = store.bulk_delete_sessions(data.ids)
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


@router.get("/api/sessions/{session_id}/export")
async def export_session(session_id: str, format: str = "markdown"):
    md = store.export_session_markdown(session_id)
    if md is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if format == "json":
        data = store.get_session(session_id)
        return JSONResponse(data)
    from starlette.responses import Response
    title = (store.get_session(session_id) or {}).get("title", "session")
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip() or "session"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.md"'},
    )
