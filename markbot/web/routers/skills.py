from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse

router = APIRouter()


def _get_loader():
    from markbot.config.loader import load_config
    from markbot.skills.core.loader import SkillLoader

    config = load_config()
    return SkillLoader(workspace=Path(config.workspace_path))


def _get_workspace() -> Path:
    from markbot.config.loader import load_config
    config = load_config()
    return Path(config.workspace_path)


def _skill_to_dict(skill) -> dict[str, Any]:
    """Convert a SkillDefinition to a JSON-serializable dict."""
    return {
        "name": skill.name,
        "description": skill.description,
        "when_to_use": skill.when_to_use,
        "is_builtin": skill.is_builtin,
        "is_always_active": skill.is_always_active,
        "state": skill.state,
        "view_count": skill.view_count,
        "use_count": skill.use_count,
        "last_activity_at": skill.last_activity_at,
        "scripts": [
            {
                "name": s.name,
                "description": s.description,
                "entry": s.entry,
                "language": s.language,
            }
            for s in (skill.scripts or [])
        ],
        "config_vars": [
            {
                "key": v.key,
                "description": v.description,
                "default": v.default,
                "prompt": v.prompt,
            }
            for v in (skill.config_vars or [])
        ],
        "conditions": {
            "requires_tools": skill.conditions.requires_tools if skill.conditions else [],
            "fallback_for_tools": skill.conditions.fallback_for_tools if skill.conditions else [],
        },
    }


@router.get("/api/skills")
async def list_skills():
    try:
        loader = _get_loader()
        skills = loader.load_all()
        return JSONResponse({"skills": [_skill_to_dict(s) for s in skills]})
    except Exception as e:
        return JSONResponse({"skills": [], "error": str(e)}, status_code=500)


@router.get("/api/skills/{name}")
async def get_skill_detail(name: str):
    try:
        loader = _get_loader()
        skill_path = loader.get_skill_path(name)
        if not skill_path:
            return JSONResponse({"error": "Skill not found"}, status_code=404)

        # Load skill definition
        skills = loader.load_all()
        skill = next((s for s in skills if s.name == name), None)
        if not skill:
            return JSONResponse({"error": "Skill not found"}, status_code=404)

        result = _skill_to_dict(skill)

        # Read SKILL.md content
        skill_file = skill_path / "SKILL.md"
        if skill_file.exists():
            result["content"] = skill_file.read_text(encoding="utf-8")
        else:
            result["content"] = ""

        result["path"] = str(skill_path)

        # List files in skill directory
        files = []
        for f in sorted(skill_path.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                files.append(str(f.relative_to(skill_path)))
        result["files"] = files

        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    when_to_use: str = ""
    content: str = ""


@router.post("/api/skills")
async def create_skill(data: SkillCreate):
    """Create a new skill in the workspace."""
    try:
        from markbot.skills.core.loader import SkillLoader

        # Validate name
        error = SkillLoader.validate_name(data.name)
        if error:
            return JSONResponse({"error": error}, status_code=400)

        workspace = _get_workspace()
        skill_dir = workspace / "skills" / data.name
        if skill_dir.exists():
            return JSONResponse({"error": f"Skill '{data.name}' already exists"}, status_code=400)

        skill_dir.mkdir(parents=True, exist_ok=True)

        # Build SKILL.md with frontmatter
        frontmatter_lines = ["---", f"name: {data.name}"]
        if data.description:
            frontmatter_lines.append(f"description: {data.description}")
        if data.when_to_use:
            frontmatter_lines.append(f"when_to_use: {data.when_to_use}")
        frontmatter_lines.append("---")
        frontmatter_lines.append("")

        skill_content = "\n".join(frontmatter_lines) + (data.content or "")
        (skill_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

        # Record creation timestamp so the lifecycle grace period works.
        try:
            from markbot.skills.usage import SkillUsageStore
            usage_store = SkillUsageStore(workspace)
            usage_store.set_created_at(data.name)
        except Exception:
            pass

        return JSONResponse({"ok": True, "name": data.name})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/api/skills/{name}")
async def delete_skill(name: str):
    """Delete a workspace skill (builtin skills cannot be deleted)."""
    try:
        loader = _get_loader()
        skill_path = loader.get_skill_path(name)
        if not skill_path:
            return JSONResponse({"error": "Skill not found"}, status_code=404)

        # Check if it's a builtin skill
        skills = loader.load_all()
        skill = next((s for s in skills if s.name == name), None)
        if skill and skill.is_builtin:
            return JSONResponse({"error": "Cannot delete builtin skill"}, status_code=400)

        # Check that the skill is in the workspace, not builtin dir
        workspace = _get_workspace()
        workspace_skill = workspace / "skills" / name
        if not workspace_skill.exists():
            return JSONResponse({"error": "Skill not found in workspace"}, status_code=404)

        import shutil
        shutil.rmtree(workspace_skill)

        # Clean up usage tracking data so stale entries don't linger.
        try:
            from markbot.skills.usage import SkillUsageStore
            usage_store = SkillUsageStore(workspace)
            usage_store.remove(name)
        except Exception:
            pass

        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


class SkillUpdate(BaseModel):
    description: str | None = None
    when_to_use: str | None = None
    content: str | None = None


@router.put("/api/skills/{name}")
async def update_skill(name: str, data: SkillUpdate):
    """Update a skill's SKILL.md content."""
    try:
        loader = _get_loader()
        skill_path = loader.get_skill_path(name)
        if not skill_path:
            return JSONResponse({"error": "Skill not found"}, status_code=404)

        skill_file = skill_path / "SKILL.md"
        if not skill_file.exists():
            return JSONResponse({"error": "SKILL.md not found"}, status_code=404)

        if data.content is not None:
            skill_file.write_text(data.content, encoding="utf-8")
        else:
            # Update frontmatter fields
            content = skill_file.read_text(encoding="utf-8")
            if data.description is not None:
                content = _update_frontmatter_field(content, "description", data.description)
            if data.when_to_use is not None:
                content = _update_frontmatter_field(content, "when_to_use", data.when_to_use)
            skill_file.write_text(content, encoding="utf-8")

        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _update_frontmatter_field(content: str, field: str, value: str) -> str:
    """Update or add a field in the YAML frontmatter."""
    if not content.startswith("---"):
        return content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return content

    frontmatter = parts[1]
    body = parts[2]

    lines = frontmatter.split("\n")
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{field}:"):
            new_lines.append(f"{field}: {value}")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{field}: {value}")

    return "---" + "\n".join(new_lines) + "---" + body
