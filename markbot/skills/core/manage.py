"""Skill management tool — agent-managed skill creation & editing.

Allows the agent to create, update, and delete skills, turning successful
approaches into reusable procedural knowledge.

Actions:
  create      -- Create a new skill (SKILL.md + directory structure)
  edit        -- Replace the SKILL.md content of a skill (full rewrite)
  patch       -- Targeted find-and-replace within SKILL.md or a supporting file
  delete      -- Remove a skill entirely
  write_file  -- Add/overwrite a supporting file (reference, template, script, asset)
  remove_file -- Remove a supporting file from a skill
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.skills.core.loader import SkillLoader
from markbot.skills.core.scanner import SecurityScanner
from markbot.skills.usage import SkillUsageStore
from markbot.tools.base import BaseTool
from markbot.types.permission import PermissionDecision
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter


class SkillManageTool(BaseTool):
    """Tool for agent-managed skill creation and editing.

    Skills are the agent's procedural memory: they capture *how to do a specific
    type of task* based on proven experience. This tool lets the agent turn
    successful approaches into reusable skills and keep them up to date.
    """

    def __init__(self, workspace: Path):
        self._workspace = workspace
        self._workspace_skills = workspace / "skills"
        self._usage_store = SkillUsageStore(workspace)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="skill_manage",
            description=(
                "Create, edit, or delete skills — the agent's procedural memory. "
                "Use 'create' to save a new workflow as a skill after completing a complex task. "
                "Use 'edit' to rewrite a skill's SKILL.md. "
                "Use 'patch' for targeted fixes within a skill file. "
                "Use 'delete' to remove a skill. "
                "Use 'write_file' to add supporting files (references, templates, scripts, assets). "
                "Use 'remove_file' to delete a supporting file. "
                "After difficult/iterative tasks, offer to save as a skill. "
                "If a skill you loaded was missing steps or had wrong commands, update it with patch."
            ),
            parameters=[
                ToolParameter(
                    name="action",
                    type="string",
                    description="Action to perform: create, edit, patch, delete, write_file, remove_file",
                    required=True,
                    enum=["create", "edit", "patch", "delete", "write_file", "remove_file"],
                ),
                ToolParameter(
                    name="name",
                    type="string",
                    description="Skill name (lowercase, hyphens, digits). Must match [a-z0-9][a-z0-9._-]*",
                    required=True,
                ),
                ToolParameter(
                    name="content",
                    type="string",
                    description="Full SKILL.md content for 'create' and 'edit' actions. Must include YAML frontmatter with name and description.",
                    required=False,
                ),
                ToolParameter(
                    name="old_string",
                    type="string",
                    description="Text to find for 'patch' action. Must be unique within the file unless replace_all is true.",
                    required=False,
                ),
                ToolParameter(
                    name="new_string",
                    type="string",
                    description="Replacement text for 'patch' action. Use empty string to delete matched text.",
                    required=False,
                ),
                ToolParameter(
                    name="file_path",
                    type="string",
                    description="Supporting file path relative to skill dir (e.g. 'references/api.md', 'scripts/helper.py'). Only for patch/write_file/remove_file. Must be under references/, templates/, scripts/, or assets/.",
                    required=False,
                ),
                ToolParameter(
                    name="file_content",
                    type="string",
                    description="Content for write_file action.",
                    required=False,
                ),
                ToolParameter(
                    name="replace_all",
                    type="boolean",
                    description="Replace all occurrences of old_string in patch action. Default: false.",
                    required=False,
                    default=False,
                ),
            ],
            is_read_only=False,
            is_destructive=True,
        )

    @property
    def is_enabled(self) -> bool:
        return True

    async def check_permission(
        self, params: dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        action = params.get("action", "")
        if action in ("create", "edit", "patch", "write_file"):
            return PermissionDecision(behavior="ask")
        if action in ("delete", "remove_file"):
            return PermissionDecision(behavior="ask")
        return PermissionDecision(behavior="allow")

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        action = params.get("action", "").strip()
        name = params.get("name", "").strip()

        if not action:
            return "Error: 'action' parameter is required."
        if not name and action != "create":
            return "Error: 'name' parameter is required."

        dispatch = {
            "create": self._create_skill,
            "edit": self._edit_skill,
            "patch": self._patch_skill,
            "delete": self._delete_skill,
            "write_file": self._write_file,
            "remove_file": self._remove_file,
        }

        handler = dispatch.get(action)
        if not handler:
            return f"Error: Unknown action '{action}'. Valid actions: {', '.join(dispatch.keys())}"

        try:
            return handler(params)
        except Exception as e:
            logger.exception("skill_manage {} failed for {}", action, name)
            return f"Error: {e}"

    def _find_skill_dir(self, name: str) -> Path | None:
        """Find a skill directory by name, checking workspace then builtin."""
        ws = self._workspace_skills / name
        if ws.exists() and (ws / "SKILL.md").exists():
            return ws

        from markbot.skills.core.loader import BUILTIN_SKILLS_DIR
        builtin = BUILTIN_SKILLS_DIR / name
        if builtin.exists() and (builtin / "SKILL.md").exists():
            return builtin

        return None

    def _is_workspace_skill(self, skill_dir: Path) -> bool:
        """Check if a skill is in the workspace (editable) vs builtin (read-only)."""
        try:
            skill_dir.resolve().relative_to(self._workspace_skills.resolve())
            return True
        except ValueError:
            return False

    def _create_skill(self, params: dict[str, Any]) -> str:
        name = params.get("name", "").strip()
        content = params.get("content", "")

        err = SkillLoader.validate_name(name)
        if err:
            return f"Error: {err}"

        err = SkillLoader.validate_frontmatter(content)
        if err:
            return f"Error: {err}"

        err = SkillLoader.validate_content_size(content)
        if err:
            return f"Error: {err}"

        existing = self._find_skill_dir(name)
        if existing:
            return f"Error: A skill named '{name}' already exists at {existing}."

        skill_dir = self._workspace_skills / name
        skill_dir.mkdir(parents=True, exist_ok=True)

        skill_md = skill_dir / "SKILL.md"
        self._atomic_write(skill_md, content)

        scan_error = self._security_scan(skill_dir)
        if scan_error:
            shutil.rmtree(skill_dir, ignore_errors=True)
            return f"Error: Security scan blocked this skill: {scan_error}"

        # Record creation timestamp in usage store
        self._usage_store.set_created_at(name)

        return (
            f"Skill '{name}' created at {skill_dir}.\n"
            f"To add supporting files, use skill_manage(action='write_file', "
            f"name='{name}', file_path='references/example.md', file_content='...')"
        )

    def _edit_skill(self, params: dict[str, Any]) -> str:
        name = params.get("name", "").strip()
        content = params.get("content", "")

        err = SkillLoader.validate_frontmatter(content)
        if err:
            return f"Error: {err}"

        err = SkillLoader.validate_content_size(content)
        if err:
            return f"Error: {err}"

        skill_dir = self._find_skill_dir(name)
        if not skill_dir:
            return f"Error: Skill '{name}' not found. Use skills_list to see available skills."

        if not self._is_workspace_skill(skill_dir):
            return (
                f"Error: Skill '{name}' is a built-in skill and cannot be modified. "
                f"Create a new skill with a different name instead."
            )

        skill_md = skill_dir / "SKILL.md"
        original = skill_md.read_text(encoding="utf-8") if skill_md.exists() else None
        self._atomic_write(skill_md, content)

        scan_error = self._security_scan(skill_dir)
        if scan_error:
            if original is not None:
                self._atomic_write(skill_md, original)
            return f"Error: Security scan blocked this edit: {scan_error}"

        return f"Skill '{name}' updated."

    def _patch_skill(self, params: dict[str, Any]) -> str:
        name = params.get("name", "").strip()
        old_string = params.get("old_string", "")
        new_string = params.get("new_string", "")
        file_path = params.get("file_path", "")
        replace_all = params.get("replace_all", False)

        if not old_string:
            return "Error: 'old_string' is required for 'patch' action."
        if new_string is None:
            return "Error: 'new_string' is required for 'patch'. Use empty string to delete matched text."

        skill_dir = self._find_skill_dir(name)
        if not skill_dir:
            return f"Error: Skill '{name}' not found."

        if not self._is_workspace_skill(skill_dir):
            return f"Error: Skill '{name}' is a built-in skill and cannot be modified."

        if file_path:
            err = SkillLoader.validate_file_path(file_path)
            if err:
                return f"Error: {err}"
            target = skill_dir / file_path
            resolved = target.resolve()
            skill_dir_resolved = skill_dir.resolve()
            try:
                resolved.relative_to(skill_dir_resolved)
            except ValueError:
                return "Error: File path escapes the skill directory."
        else:
            target = skill_dir / "SKILL.md"

        if not target.exists():
            return f"Error: File not found: {target.relative_to(skill_dir)}"

        content = target.read_text(encoding="utf-8")

        count = content.count(old_string)
        if count == 0:
            preview = content[:500] + ("..." if len(content) > 500 else "")
            return (
                f"Error: 'old_string' not found in {target.relative_to(skill_dir)}.\n"
                f"File preview:\n{preview}"
            )
        if count > 1 and not replace_all:
            return (
                f"Error: 'old_string' found {count} times in {target.relative_to(skill_dir)}. "
                f"Set replace_all=true to replace all occurrences, or provide a more specific old_string."
            )

        new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

        if not file_path:
            err = SkillLoader.validate_frontmatter(new_content)
            if err:
                return f"Error: Patch would break SKILL.md structure: {err}"

        err = SkillLoader.validate_content_size(new_content, label=str(target.relative_to(skill_dir)))
        if err:
            return f"Error: {err}"

        self._atomic_write(target, new_content)

        if not file_path:
            scan_error = self._security_scan(skill_dir)
            if scan_error:
                self._atomic_write(target, content)
                return f"Error: Security scan blocked this patch: {scan_error}"

        return f"Patched {target.relative_to(skill_dir)} ({count} replacement{'s' if count > 1 else ''})."

    def _delete_skill(self, params: dict[str, Any]) -> str:
        name = params.get("name", "").strip()

        skill_dir = self._find_skill_dir(name)
        if not skill_dir:
            return f"Error: Skill '{name}' not found."

        if not self._is_workspace_skill(skill_dir):
            return f"Error: Cannot delete built-in skill '{name}'."

        shutil.rmtree(skill_dir)
        self._usage_store.remove(name)
        return f"Skill '{name}' deleted."

    def _write_file(self, params: dict[str, Any]) -> str:
        name = params.get("name", "").strip()
        file_path = params.get("file_path", "").strip()
        file_content = params.get("file_content", "")

        err = SkillLoader.validate_file_path(file_path)
        if err:
            return f"Error: {err}"

        skill_dir = self._find_skill_dir(name)
        if not skill_dir:
            return f"Error: Skill '{name}' not found."

        if not self._is_workspace_skill(skill_dir):
            return f"Error: Skill '{name}' is a built-in skill and cannot be modified."

        target = skill_dir / file_path
        resolved = target.resolve()
        skill_dir_resolved = skill_dir.resolve()
        try:
            resolved.relative_to(skill_dir_resolved)
        except ValueError:
            return "Error: File path escapes the skill directory."

        if target.is_symlink():
            return "Error: Symlinks are not allowed."

        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, file_content)

        return f"Written {file_path} in skill '{name}'."

    def _remove_file(self, params: dict[str, Any]) -> str:
        name = params.get("name", "").strip()
        file_path = params.get("file_path", "").strip()

        err = SkillLoader.validate_file_path(file_path)
        if err:
            return f"Error: {err}"

        skill_dir = self._find_skill_dir(name)
        if not skill_dir:
            return f"Error: Skill '{name}' not found."

        if not self._is_workspace_skill(skill_dir):
            return f"Error: Skill '{name}' is a built-in skill and cannot be modified."

        target = skill_dir / file_path
        if not target.exists():
            return f"Error: File not found: {file_path}"

        resolved = target.resolve()
        skill_dir_resolved = skill_dir.resolve()
        try:
            resolved.relative_to(skill_dir_resolved)
        except ValueError:
            return "Error: File path escapes the skill directory."

        target.unlink()
        return f"Removed {file_path} from skill '{name}'."

    @staticmethod
    def _atomic_write(file_path: Path, content: str) -> None:
        """Atomically write text content to a file."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            dir=str(file_path.parent),
            prefix=f".{file_path.name}.tmp.",
            suffix="",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(temp_path, file_path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _security_scan(skill_dir: Path) -> str | None:
        """Scan a skill directory for security issues. Returns error string or None."""
        scanner = SecurityScanner()
        all_findings = []

        for f in sorted(skill_dir.rglob("*")):
            if not f.is_file() or f.is_symlink():
                continue
            if f.name == "SKILL.md":
                continue

            ext = f.suffix.lower()
            lang_map = {".py": "python", ".sh": "shell", ".bash": "bash", ".js": "node", ".mjs": "node"}
            language = lang_map.get(ext)
            if not language:
                continue

            result = scanner.scan(f, language)
            if not result.is_safe:
                for finding in result.findings:
                    if finding.severity in ("critical", "high"):
                        all_findings.append(
                            f"  {f.relative_to(skill_dir)}: Line {finding.line}: "
                            f"[{finding.severity}] {finding.message}"
                        )

        if all_findings:
            return "Security scan found dangerous patterns:\n" + "\n".join(all_findings)
        return None
