from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse
from typing import Any

router = APIRouter()

_cron_jobs: dict[str, dict[str, Any]] = {}


class CronCreate(BaseModel):
    name: str
    schedule: str
    command: str


class CronUpdate(BaseModel):
    name: str | None = None
    schedule: str | None = None
    command: str | None = None
    enabled: bool | None = None


@router.get("/api/cron/jobs")
async def list_cron_jobs():
    return JSONResponse({"jobs": list(_cron_jobs.values())})


@router.post("/api/cron/jobs")
async def create_cron_job(data: CronCreate):
    import secrets
    job_id = secrets.token_urlsafe(8)
    _cron_jobs[job_id] = {
        "id": job_id,
        "name": data.name,
        "schedule": data.schedule,
        "command": data.command,
        "enabled": True,
    }
    return JSONResponse({"ok": True, "id": job_id})


@router.put("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, data: CronUpdate):
    job = _cron_jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if data.name is not None:
        job["name"] = data.name
    if data.schedule is not None:
        job["schedule"] = data.schedule
    if data.command is not None:
        job["command"] = data.command
    if data.enabled is not None:
        job["enabled"] = data.enabled
    return JSONResponse({"ok": True})


@router.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str):
    _cron_jobs.pop(job_id, None)
    return JSONResponse({"ok": True})


@router.post("/api/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str):
    job = _cron_jobs.get(job_id)
    if job:
        job["enabled"] = False
    return JSONResponse({"ok": True})


@router.post("/api/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str):
    job = _cron_jobs.get(job_id)
    if job:
        job["enabled"] = True
    return JSONResponse({"ok": True})


@router.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str):
    return JSONResponse({"ok": True, "message": "Job triggered"})
