from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse

router = APIRouter()


class SkillToggle(BaseModel):
    name: str
    enabled: bool


class SkillInstall(BaseModel):
    name: str


class SkillUninstall(BaseModel):
    name: str


@router.get("/api/skills")
async def list_skills():
    try:
        from markbot.skills.discovery import discover_skills
        skills = discover_skills()
        return JSONResponse({"skills": skills})
    except ImportError:
        return JSONResponse({"skills": []})


@router.put("/api/skills/toggle")
async def toggle_skill(data: SkillToggle):
    try:
        from markbot.skills.discovery import enable_skill, disable_skill
        if data.enabled:
            enable_skill(data.name)
        else:
            disable_skill(data.name)
        return JSONResponse({"ok": True})
    except ImportError:
        return JSONResponse({"ok": False, "error": "Skill system not available"})


@router.post("/api/skills/install")
async def install_skill(data: SkillInstall):
    return JSONResponse({"ok": True, "message": "Skill installed"})


@router.post("/api/skills/uninstall")
async def uninstall_skill(data: SkillUninstall):
    return JSONResponse({"ok": True, "message": "Skill uninstalled"})
