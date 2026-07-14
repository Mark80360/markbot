from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse

router = APIRouter()

# Shared CronService instance set by the web server on startup.
# When set, all router endpoints operate on the same in-memory store as the
# running agent loop, avoiding data races between the router and the timer.
_shared_cron_service = None


def set_cron_service(svc) -> None:
    """Inject the shared CronService (called by web server after agent loop creation)."""
    global _shared_cron_service
    _shared_cron_service = svc


def _get_cron_service():
    """Get the CronService instance.

    Returns the shared instance when available (so that router operations and
    the agent loop's timer operate on the same in-memory store). Falls back to
    constructing a standalone instance pointing at the persistent store.
    """
    if _shared_cron_service is not None:
        return _shared_cron_service

    from markbot.config.loader import load_config
    from markbot.config.paths import get_cron_dir
    from markbot.schedule.cron import CronService

    config = load_config()
    store_path = get_cron_dir(config.workspace_path) / "jobs.json"
    return CronService(store_path)


def _job_to_dict(job) -> dict[str, Any]:
    """Convert a CronJob dataclass to a JSON-serializable dict."""
    schedule = job.schedule
    if schedule.kind == "cron":
        schedule_str = schedule.expr or ""
        tz = schedule.tz or ""
    elif schedule.kind == "every":
        every_s = (schedule.every_ms or 0) / 1000
        schedule_str = f"every {every_s}s"
        tz = ""
    elif schedule.kind == "at":
        schedule_str = f"at {schedule.at_ms}"
        tz = ""
    else:
        schedule_str = ""
        tz = ""

    run_history = []
    for rec in (job.state.run_history or []):
        run_history.append({
            "run_at_ms": rec.run_at_ms,
            "status": rec.status,
            "duration_ms": rec.duration_ms,
            "error": rec.error,
        })

    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule": schedule_str,
        "schedule_kind": schedule.kind,
        "schedule_expr": schedule.expr if schedule.kind == "cron" else None,
        "schedule_tz": tz,
        "command": job.payload.message,
        "deliver": job.payload.deliver,
        "channel": job.payload.channel,
        "next_run_at_ms": job.state.next_run_at_ms,
        "last_run_at_ms": job.state.last_run_at_ms,
        "last_status": job.state.last_status,
        "last_error": job.state.last_error,
        "run_history": run_history,
        "created_at_ms": job.created_at_ms,
        "updated_at_ms": job.updated_at_ms,
    }


class CronCreate(BaseModel):
    name: str
    schedule: str  # cron expression, e.g. "*/5 * * * *"
    command: str
    tz: str | None = None


class CronUpdate(BaseModel):
    name: str | None = None
    schedule: str | None = None
    command: str | None = None
    enabled: bool | None = None
    tz: str | None = None


@router.get("/api/cron/jobs")
async def list_cron_jobs():
    try:
        cron = _get_cron_service()
        jobs = cron.list_jobs(include_disabled=True)
        return JSONResponse({"jobs": [_job_to_dict(j) for j in jobs]})
    except Exception as e:
        return JSONResponse({"jobs": [], "error": str(e)}, status_code=500)


@router.post("/api/cron/jobs")
async def create_cron_job(data: CronCreate):
    from markbot.schedule.cron import CronSchedule
    try:
        cron = _get_cron_service()
        schedule = CronSchedule(kind="cron", expr=data.schedule, tz=data.tz)
        job = cron.add_job(
            name=data.name,
            schedule=schedule,
            message=data.command,
        )
        return JSONResponse({"ok": True, "id": job.id, "job": _job_to_dict(job)})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, data: CronUpdate):
    try:
        cron = _get_cron_service()
        job = cron.get_job(job_id)
        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)

        # Toggle enabled state via the dedicated method
        if data.enabled is not None and data.enabled != job.enabled:
            cron.enable_job(job_id, data.enabled)
            # Re-fetch the updated job
            job = cron.get_job(job_id)
            if job is None:
                return JSONResponse({"error": "Job disappeared after toggle"}, status_code=500)

        # Update fields in place to preserve the job ID
        changed = False
        if data.name is not None and data.name != job.name:
            job.name = data.name
            changed = True
        if data.command is not None and data.command != job.payload.message:
            job.payload.message = data.command
            changed = True
        if data.schedule is not None or data.tz is not None:
            from markbot.schedule.cron import CronSchedule, _compute_next_run, _now_ms, _validate_schedule_for_add
            new_expr = data.schedule if data.schedule is not None else (job.schedule.expr or "")
            new_tz = data.tz if data.tz is not None else job.schedule.tz
            new_schedule = CronSchedule(kind="cron", expr=new_expr, tz=new_tz)
            # Validate cron expression and timezone before applying
            _validate_schedule_for_add(new_schedule)
            job.schedule = new_schedule
            # Recompute next run for the new schedule
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms()) if job.enabled else None
            changed = True

        if changed:
            job.updated_at_ms = int(__import__("time").time() * 1000)
            # Persist changes by saving the store
            cron._save_store()
            cron._arm_timer()

        return JSONResponse({"ok": True, "job": _job_to_dict(job)})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str):
    try:
        cron = _get_cron_service()
        removed = cron.remove_job(job_id)
        return JSONResponse({"ok": removed})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str):
    try:
        cron = _get_cron_service()
        job = cron.enable_job(job_id, enabled=False)
        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        return JSONResponse({"ok": True, "job": _job_to_dict(job)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str):
    try:
        cron = _get_cron_service()
        job = cron.enable_job(job_id, enabled=True)
        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        return JSONResponse({"ok": True, "job": _job_to_dict(job)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str):
    try:
        cron = _get_cron_service()
        # run_job is async and executes the on_job callback if set.
        # The standalone CronService has no on_job callback, so this just
        # records the run in history. For real execution, the agent loop's
        # cron service should be used.
        ran = await cron.run_job(job_id, force=True)
        if not ran:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        job = cron.get_job(job_id)
        return JSONResponse({"ok": True, "job": _job_to_dict(job) if job else None})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/cron/status")
async def cron_status():
    try:
        cron = _get_cron_service()
        return JSONResponse(cron.status())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
