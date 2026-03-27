"""Skills loader for agent capabilities.

Enhanced with executable script support. Skills can now define executable
scripts in their SKILL.md metadata that get automatically registered as tools.
"""

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from markbot.agent.tools.registry import ToolRegistry
from markbot.agent.skill_execution import SkillScript, SecurityScanner, ScanResult, Finding

# Import PyYAML for proper YAML parsing
try:
    import yaml
    def _parse_yaml(yaml_content: str) -> dict | None:
        try:
            return yaml.safe_load(yaml_content)
        except Exception:
            return None
except ImportError:
    def _parse_yaml(yaml_content: str) -> dict | None:
        return None

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"

logger = logging.getLogger(__name__)


class SkillsLoader:
    """Enhanced loader for agent skills with executable script support.

    Skills are markdown files (SKILL.md) that can declare executable scripts
    in their YAML frontmatter. These scripts are automatically registered
    as tools that the agent can invoke.

    Example SKILL.md format:
    ---
    name: my-skill
    description: "Code analysis tools"
    metadata:
      markbot:
        executable: true
        scripts:
          - name: "format"
            description: "Format code"
            entry: "scripts/format.py"
            language: "python"
            parameters:
              type: object
              properties:
                path:
                  type: string
                  description: "Path to format"
              required: ["path"]
            sandbox:
              allowed_paths: ["{workspace}"]
              timeout: 60
    ---
    """

    def __init__(
        self,
        workspace: Path,
        builtin_skills_dir: Path | None = None,
        tool_registry: Optional[ToolRegistry] = None,
    ):
        """Initialize the skills loader.

        Args:
            workspace: Workspace directory for user skills
            builtin_skills_dir: Directory containing built-in skills
            tool_registry: Registry to register executable scripts as tools
        """
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.tool_registry = tool_registry
        self._scanner = SecurityScanner()
        self._loaded_scripts: dict[str, SkillScript] = {}

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source', 'executable'.
        """
        skills = []

        # Workspace skills (highest priority)
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        meta = self.get_skill_metadata(skill_dir.name)
                        executable = self._is_executable_skill(meta)
                        skills.append({
                            "name": skill_dir.name,
                            "path": str(skill_file),
                            "source": "workspace",
                            "executable": str(executable).lower(),
                        })

        # Built-in skills
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        meta = self.get_skill_metadata(skill_dir.name)
                        executable = self._is_executable_skill(meta)
                        skills.append({
                            "name": skill_dir.name,
                            "path": str(skill_file),
                            "source": "builtin",
                            "executable": str(executable).lower(),
                        })

        # Filter by requirements
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def list_executable_scripts(self, skill_name: Optional[str] = None) -> list[dict[str, Any]]:
        """List all executable scripts across all skills.

        Args:
            skill_name: If specified, only list scripts for this skill.

        Returns:
            List of script info dicts.
        """
        scripts = []

        if skill_name:
            # Scripts from specific skill
            meta = self.get_skill_metadata(skill_name)
            if meta:
                scripts_info = self._get_scripts_from_metadata(meta, skill_name)
                for info in scripts_info:
                    info["skill_name"] = skill_name
                scripts.extend(scripts_info)
        else:
            # Scripts from all skills
            for skill in self.list_skills():
                meta = self.get_skill_metadata(skill["name"])
                if meta and self._is_executable_skill(meta):
                    scripts_info = self._get_scripts_from_metadata(meta, skill["name"])
                    for info in scripts_info:
                        info["skill_name"] = skill["name"]
                    scripts.extend(scripts_info)

        return scripts

    def load_skill(self, name: str) -> str | None:
        """Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        # Check workspace first
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return workspace_skill.read_text(encoding="utf-8")

        # Check built-in
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """Build a summary of all skills for agent context.

        Returns:
            XML-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)
            executable = s.get("executable", "false")

            lines.append(f'  <skill available="{str(available).lower()}" executable="{executable}">')
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # List executable scripts
            if executable == "true":
                scripts = self.list_executable_scripts(s["name"])
                if scripts:
                    lines.append("    <scripts>")
                    for script in scripts:
                        script_name = escape_xml(script["name"])
                        script_desc = escape_xml(script["description"])
                        lines.append(f'      <script name="{script_name}">{script_desc}</script>')
                    lines.append("    </scripts>")

            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def register_executable_scripts(self, skill_name: str) -> list[SkillScript]:
        """Register all executable scripts from a skill as tools.

        Args:
            skill_name: Name of the skill.

        Returns:
            List of registered SkillScript instances.
        """
        if self.tool_registry is None:
            logger.warning("No tool registry provided, cannot register scripts")
            return []

        meta = self.get_skill_metadata(skill_name)
        if not meta or not self._is_executable_skill(meta):
            return []

        skill_path = self._get_skill_path(skill_name)
        if not skill_path:
            logger.warning(f"Could not find skill path for: {skill_name}")
            return []

        scripts = self._get_scripts_from_metadata(meta, skill_name)
        registered = []

        for script_info in scripts:
            tool_name = f"{skill_name}.{script_info['name']}"

            # Skip if already registered
            if tool_name in self._loaded_scripts:
                continue

            # Resolve entry path
            entry_path = skill_path / script_info["entry"]
            if not entry_path.exists():
                logger.warning(f"Script entry not found: {entry_path}")
                continue

            # Create SkillScript tool
            skill_script = SkillScript(
                name=tool_name,
                script_name=script_info["name"],
                description=script_info["description"],
                entry=entry_path,
                language=script_info["language"],
                parameters=script_info.get("parameters", {"type": "object", "properties": {}}),
                sandbox_config=script_info.get("sandbox"),
                skill_path=skill_path,
            )

            # Register with tool registry
            self.tool_registry.register(skill_script)
            self._loaded_scripts[tool_name] = skill_script
            registered.append(skill_script)
            logger.info(f"Registered skill script: {tool_name}")

        return registered

    def register_all_executable_scripts(self) -> list[SkillScript]:
        """Register all executable scripts from all available skills.
        
        This is called during initialization to make skill scripts available
        as tools that the agent can invoke.
        
        Returns:
            List of all registered SkillScript instances.
        """
        all_registered = []
        
        if self.tool_registry is None:
            logger.debug("No tool registry provided, skipping script registration")
            return all_registered
        
        for skill in self.list_skills(filter_unavailable=True):
            if skill.get("executable") == "true":
                try:
                    scripts = self.register_executable_scripts(skill["name"])
                    all_registered.extend(scripts)
                except Exception as e:
                    logger.warning(f"Failed to register scripts for skill '{skill['name']}': {e}")
        
        if all_registered:
            logger.info(f"Registered {len(all_registered)} skill scripts from {len(set(s.name.split('.')[0] for s in all_registered))} skills")
        
        return all_registered

    def scan_script(self, skill_name: str, script_name: str) -> ScanResult:
        """Scan a script for security issues.

        Args:
            skill_name: Name of the skill.
            script_name: Name of the script.

        Returns:
            ScanResult with findings.
        """
        # Find the script
        scripts = self.list_executable_scripts(skill_name)
        script_info = None

        for s in scripts:
            if s["name"] == script_name:
                script_info = s
                break

        if not script_info:
            return ScanResult(
                is_safe=False,
                findings=[Finding(
                    line=0,
                    pattern="",
                    severity="critical",
                    message=f"Script '{script_name}' not found in skill '{skill_name}'",
                )],
            )

        skill_path = self._get_skill_path(skill_name)
        if not skill_path:
            return ScanResult(
                is_safe=False,
                findings=[Finding(
                    line=0,
                    pattern="",
                    severity="critical",
                    message=f"Skill path not found: {skill_name}",
                )],
            )

        entry_path = skill_path / script_info["entry"]
        return self._scanner.scan(entry_path, script_info["language"])

    def validate_skill(self, skill_name: str) -> list[str]:
        """Validate a skill's metadata and scripts.

        Args:
            skill_name: Name of the skill to validate.

        Returns:
            List of validation errors (empty if valid).
        """
        errors = []

        meta = self.get_skill_metadata(skill_name)
        if not meta:
            return [f"Skill '{skill_name}' not found"]

        if not self._is_executable_skill(meta):
            return []  # Not an executable skill, nothing to validate

        skill_path = self._get_skill_path(skill_name)
        if not skill_path:
            return [f"Skill path not found: {skill_name}"]

        # Validate scripts
        scripts = self._get_scripts_from_metadata(meta, skill_name)

        if not scripts:
            errors.append("No scripts defined in metadata")

        for script in scripts:
            # Check required fields
            if not script.get("name"):
                errors.append("Script missing 'name' field")
                continue

            if not script.get("entry"):
                errors.append(f"Script '{script['name']}' missing 'entry' field")
                continue

            if not script.get("language"):
                errors.append(f"Script '{script['name']}' missing 'language' field")
                continue

            # Check entry file exists
            entry_path = skill_path / script["entry"]
            if not entry_path.exists():
                errors.append(f"Script entry not found: {entry_path}")

            # Validate JSON Schema
            params = script.get("parameters", {})
            if params and "type" not in params:
                errors.append(f"Script '{script['name']}' has invalid parameters schema")

        return errors

    def execute_script(
        self,
        skill_name: str,
        script_name: str,
        args: dict[str, Any],
    ) -> str:
        """Execute a skill script with given arguments.

        This is a synchronous wrapper for async execution.

        Args:
            skill_name: Name of the skill.
            script_name: Name of the script.
            args: Arguments to pass to the script.

        Returns:
            Script output or error message.
        """
        import asyncio

        tool_name = f"{skill_name}.{script_name}"

        # Check if already registered
        if tool_name in self._loaded_scripts:
            skill_script = self._loaded_scripts[tool_name]
        else:
            # Register scripts and find the one we need
            self.register_executable_scripts(skill_name)
            skill_script = self._loaded_scripts.get(tool_name)

        if not skill_script:
            return f"Error: Script '{script_name}' not found in skill '{skill_name}'"

        # Execute
        try:
            return asyncio.run(skill_script.execute(**args))
        except Exception as e:
            return f"Error: Failed to execute script: {e}"

    def _get_skill_path(self, name: str) -> Path | None:
        """Get the path to a skill directory."""
        workspace_skill = self.workspace_skills / name
        if workspace_skill.exists():
            return workspace_skill

        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name
            if builtin_skill.exists():
                return builtin_skill

        return None

    def _is_executable_skill(self, meta: dict | None) -> bool:
        """Check if skill metadata indicates it's executable."""
        if not meta:
            return False

        markbot_meta = self._parse_markbot_metadata(meta.get("metadata", ""))
        return markbot_meta.get("executable", False)

    def _get_scripts_from_metadata(
        self, meta: dict, skill_name: str
    ) -> list[dict[str, Any]]:
        """Extract scripts list from skill metadata."""
        markbot_meta = self._parse_markbot_metadata(meta.get("metadata", ""))
        scripts = markbot_meta.get("scripts", [])

        # Convert workspace placeholder in sandbox config
        skill_path = self._get_skill_path(skill_name)
        for script in scripts:
            if "sandbox" in script and skill_path:
                sandbox_config = script["sandbox"]
                if "allowed_paths" in sandbox_config:
                    sandbox_config["allowed_paths"] = [
                        p.replace("{workspace}", str(self.workspace))
                        for p in sandbox_config["allowed_paths"]
                    ]

        return scripts

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_markbot_metadata(self, raw: str | dict) -> dict:
        """Parse skill metadata from frontmatter.
        
        Handles both JSON string format and nested dictionary format.
        """
        # If already a dict, it's from nested YAML
        if isinstance(raw, dict):
            return raw.get("markbot", raw.get("openclaw", {}))
        
        # Try to parse as JSON string (legacy format)
        try:
            data = json.loads(raw) if raw else {}
            return data.get("markbot", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met."""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """Get markbot metadata for a skill."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_markbot_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_markbot_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """Get metadata from a skill's frontmatter.
        
        Returns a dictionary with both flat key-value pairs and nested structures.
        For nested YAML like 'metadata:\n  markbot:\n    executable: true',
        the result will contain both the raw text and parsed structure.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                yaml_content = match.group(1)
                
                # Use PyYAML if available for proper parsing
                metadata = _parse_yaml(yaml_content)
                if metadata and isinstance(metadata, dict):
                    return metadata
                # Otherwise fall back to manual parsing
                
                # Fallback to manual parser
                metadata = self._parse_yaml_frontmatter(yaml_content)
                return metadata

        return None
    
    def _parse_yaml_frontmatter(self, yaml_content: str) -> dict:
        """Parse YAML frontmatter content.
        
        Handles:
        - Simple key: value pairs
        - Nested structures (metadata: markbot: ...)
        - Lists (scripts: - name: ...)
        
        This is a simplified parser for SKILL.md frontmatter.
        """
        metadata = {}
        lines = yaml_content.split("\n")
        i = 0
        
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            
            # Skip empty lines and comments
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            
            # Check if it's a key-value pair at root level
            if ":" in line and not line.startswith(" ") and not line.startswith("\t"):
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                
                # If value is empty, it might be a nested structure or list
                if not value:
                    # Check what follows
                    j = i + 1
                    if j < len(lines):
                        next_line = lines[j]
                        # Check if it's a list (starts with -)
                        if next_line.strip().startswith("-"):
                            # It's a list
                            list_items = []
                            while j < len(lines):
                                list_line = lines[j]
                                if not list_line.strip():
                                    j += 1
                                    continue
                                if not list_line.startswith(" ") and not list_line.startswith("\t"):
                                    break
                                if list_line.strip().startswith("-"):
                                    # Extract list item
                                    item_content = self._extract_list_item(lines, j)
                                    list_items.append(item_content)
                                    # Skip consumed lines
                                    while j < len(lines):
                                        check_line = lines[j]
                                        if not check_line.strip():
                                            j += 1
                                        elif check_line.startswith(" ") or check_line.startswith("\t"):
                                            j += 1
                                        else:
                                            break
                                    i = j - 1
                                else:
                                    j += 1
                            metadata[key] = list_items
                        else:
                            # Nested structure
                            nested = {}
                            while j < len(lines):
                                next_line = lines[j]
                                if not next_line.strip():
                                    j += 1
                                    continue
                                if not next_line.startswith(" ") and not next_line.startswith("\t"):
                                    break
                                if ":" in next_line:
                                    nested_content = self._extract_nested_block(lines, j)
                                    nested.update(nested_content)
                                    j = self._find_next_line_at_indent(lines, j, 0)
                                    i = j - 1
                                else:
                                    j += 1
                            metadata[key] = nested
                else:
                    # Simple key-value
                    metadata[key] = value.strip('"\'')
            
            i += 1
        
        return metadata
    
    def _extract_list_item(self, lines: list, start: int) -> dict:
        """Extract a list item (dict) from YAML list."""
        result = {}
        i = start
        base_indent = len(lines[i]) - len(lines[i].lstrip())
        
        # Parse the first line (e.g., "- name: value")
        first_line = lines[i].strip()
        if first_line.startswith("-"):
            first_line = first_line[1:].strip()
        
        if ":" in first_line:
            key, value = first_line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value:
                result[key] = value.strip('"\'')
        
        i += 1
        
        # Parse subsequent lines at same or greater indent
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent and line.strip():
                break
            
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                
                if not value:
                    # Nested structure in list item
                    nested = {}
                    j = i + 1
                    while j < len(lines):
                        sub_line = lines[j]
                        if not sub_line.strip():
                            j += 1
                            continue
                        sub_indent = len(sub_line) - len(sub_line.lstrip())
                        if sub_indent <= current_indent and sub_line.strip():
                            break
                        if ":" in sub_line:
                            sub_key, sub_value = sub_line.split(":", 1)
                            nested[sub_key.strip()] = sub_value.strip().strip('"\'')
                        j += 1
                    result[key] = nested
                    i = j - 1
                else:
                    result[key] = value.strip('"\'')
            
            i += 1
        
        return result
    
    def _extract_nested_block(self, lines: list, start: int) -> dict:
        """Extract a nested YAML block."""
        result = {}
        i = start
        base_indent = len(lines[i]) - len(lines[i].lstrip())
        
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            
            current_indent = len(line) - len(line.lstrip())
            
            if current_indent < base_indent and line.strip():
                break
            
            if current_indent == base_indent and ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                
                if not value:
                    # Deeper nesting
                    sub = {}
                    j = i + 1
                    while j < len(lines):
                        sub_line = lines[j]
                        if not sub_line.strip():
                            j += 1
                            continue
                        
                        sub_indent = len(sub_line) - len(sub_line.lstrip())
                        if sub_indent <= current_indent and sub_line.strip():
                            break
                        
                        if sub_indent > current_indent and ":" in sub_line:
                            sub_key, sub_value = sub_line.split(":", 1)
                            sub[sub_key.strip()] = sub_value.strip().strip('"\'')
                        j += 1
                    
                    result[key] = sub
                    i = j - 1
                else:
                    result[key] = value.strip('"\'')
            
            i += 1
        
        return result
    
    def _find_next_line_at_indent(self, lines: list, start: int, target_indent: int) -> int:
        """Find the index of next line at or before target indent level."""
        i = start
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= target_indent:
                return i
            i += 1
        return len(lines)
