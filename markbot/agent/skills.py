"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


@dataclass
class SkillDirectory:
    """Represents a skill's directory structure."""

    name: str
    path: Path
    source: str  # "workspace" or "builtin"
    has_scripts: bool = False
    has_references: bool = False
    scripts_tree: dict[str, Any] = field(default_factory=dict)
    references_tree: dict[str, Any] = field(default_factory=dict)


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.

    A skill directory can contain:
    - SKILL.md: Main skill definition (required)
    - scripts/: Python/bash scripts for the skill
    - references/: Reference files (schemas, templates, etc.)
    """

    def __init__(
        self,
        workspace: Path,
        builtin_skills_dir: Path | None = None,
        enable_security_scan: bool = True,
    ):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.enable_security_scan = enable_security_scan
        self._security_scanner: Any | None = None

        # Lazy import security scanner
        if enable_security_scan:
            try:
                from markbot.security import SkillScanner

                self._security_scanner = SkillScanner()
            except ImportError:
                pass

    def _get_skill_dir(self, name: str) -> SkillDirectory | None:
        """Get skill directory info for a skill name.

        Args:
            name: Skill name.

        Returns:
            SkillDirectory if found, None otherwise.
        """
        # Check workspace first
        workspace_dir = self.workspace_skills / name
        if workspace_dir.exists() and (workspace_dir / "SKILL.md").exists():
            return self._build_skill_directory(name, workspace_dir, "workspace")

        # Check built-in
        if self.builtin_skills:
            builtin_dir = self.builtin_skills / name
            if builtin_dir.exists() and (builtin_dir / "SKILL.md").exists():
                return self._build_skill_directory(name, builtin_dir, "builtin")

        return None

    def _build_skill_directory(
        self, name: str, path: Path, source: str
    ) -> SkillDirectory:
        """Build SkillDirectory with full directory tree info."""
        scripts_dir = path / "scripts"
        references_dir = path / "references"

        return SkillDirectory(
            name=name,
            path=path,
            source=source,
            has_scripts=scripts_dir.exists() and scripts_dir.is_dir(),
            has_references=references_dir.exists() and references_dir.is_dir(),
            scripts_tree=self._build_directory_tree(scripts_dir)
            if scripts_dir.exists()
            else {},
            references_tree=self._build_directory_tree(references_dir)
            if references_dir.exists()
            else {},
        )

    def _build_directory_tree(self, directory: Path) -> dict[str, Any]:
        """Recursively build a directory tree structure.

        Args:
            directory: Directory to scan.

        Returns:
            Dictionary representing the tree structure where:
            - Files are represented as {filename: None}
            - Directories are represented as {dirname: {nested_structure}}
        """
        tree: dict[str, Any] = {}

        if not directory.exists() or not directory.is_dir():
            return tree

        for item in sorted(directory.iterdir()):
            if item.is_file():
                tree[item.name] = None
            elif item.is_dir():
                tree[item.name] = self._build_directory_tree(item)

        return tree

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, Any]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source', 'has_scripts', 'has_references'.
        """
        skills = []

        # Workspace skills (highest priority)
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skill_info = self._build_skill_directory(
                            skill_dir.name, skill_dir, "workspace"
                        )
                        skills.append(
                            {
                                "name": skill_dir.name,
                                "path": str(skill_file),
                                "source": "workspace",
                                "has_scripts": skill_info.has_scripts,
                                "has_references": skill_info.has_references,
                            }
                        )

        # Built-in skills
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(
                        s["name"] == skill_dir.name for s in skills
                    ):
                        skill_info = self._build_skill_directory(
                            skill_dir.name, skill_dir, "builtin"
                        )
                        skills.append(
                            {
                                "name": skill_dir.name,
                                "path": str(skill_file),
                                "source": "builtin",
                                "has_scripts": skill_info.has_scripts,
                                "has_references": skill_info.has_references,
                            }
                        )

        # Filter by requirements
        if filter_unavailable:
            return [
                s
                for s in skills
                if self._check_requirements(self._get_skill_meta(s["name"]))
            ]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

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

    def load_skill_with_resources(self, name: str) -> dict[str, Any] | None:
        """Load a skill with all its resources (scripts, references).

        Args:
            name: Skill name.

        Returns:
            Dictionary with 'content', 'scripts', 'references', 'directory' or None.
        """
        skill_dir = self._get_skill_dir(name)
        if not skill_dir:
            return None

        content = self.load_skill(name)
        if not content:
            return None

        result = {
            "content": content,
            "directory": str(skill_dir.path),
            "scripts": {},
            "references": {},
        }

        # Load scripts
        if skill_dir.has_scripts:
            scripts_dir = skill_dir.path / "scripts"
            result["scripts"] = self._load_directory_contents(scripts_dir)

        # Load references
        if skill_dir.has_references:
            references_dir = skill_dir.path / "references"
            result["references"] = self._load_directory_contents(references_dir)

        return result

    def _load_directory_contents(self, directory: Path) -> dict[str, str]:
        """Load all file contents from a directory recursively.

        Args:
            directory: Directory to load.

        Returns:
            Dictionary mapping relative paths to file contents.
        """
        contents: dict[str, str] = {}

        if not directory.exists():
            return contents

        for item in directory.rglob("*"):
            if item.is_file():
                try:
                    rel_path = str(item.relative_to(directory))
                    # Skip binary files
                    if self._is_text_file(item):
                        contents[rel_path] = item.read_text(encoding="utf-8")
                    else:
                        contents[rel_path] = f"[Binary file: {item.name}]"
                except (OSError, UnicodeDecodeError) as e:
                    contents[str(item.relative_to(directory))] = f"[Error reading file: {e}]"

        return contents

    def _is_text_file(self, file_path: Path) -> bool:
        """Check if a file is likely a text file."""
        text_extensions = {
            ".py",
            ".sh",
            ".bash",
            ".zsh",
            ".js",
            ".ts",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".md",
            ".txt",
            ".rst",
            ".ini",
            ".cfg",
            ".conf",
            ".xml",
            ".html",
            ".css",
            ".sql",
        }
        if file_path.suffix.lower() in text_extensions:
            return True

        # Try to read first few bytes
        try:
            with open(file_path, "rb") as f:
                chunk = f.read(1024)
                return b"\0" not in chunk
        except OSError:
            return False

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            skill_data = self.load_skill_with_resources(name)
            if skill_data:
                content = self._strip_frontmatter(skill_data["content"])
                section = f"### Skill: {name}\n\n{content}"

                # Add scripts info if present
                if skill_data.get("scripts"):
                    section += f"\n\n**Scripts available:** {len(skill_data['scripts'])} files"

                # Add references info if present
                if skill_data.get("references"):
                    section += f"\n**References available:** {len(skill_data['references'])} files"

                parts.append(section)

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

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

            lines.append(f'  <skill available="{str(available).lower()}">')
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # Add scripts/references info
            if s.get("has_scripts"):
                lines.append("    <has_scripts>true</has_scripts>")
            if s.get("has_references"):
                lines.append("    <has_references>true</has_references>")

            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

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
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end() :].strip()
        return content

    def _parse_skill_metadata(self, raw: str) -> dict:
        """Parse skill metadata JSON from frontmatter."""
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """Get skill metadata from frontmatter."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_skill_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            if meta.get("always") == "true" or meta.get("always") is True:
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict with standard fields: description, trigger, always, requires.
        """
        content = self.load_skill(name)
        if not content or not content.startswith("---"):
            return None

        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return None

        metadata = {}
        for line in match.group(1).split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip().strip('"\'')

            # Parse requires as JSON if present
            if key == "requires":
                metadata[key] = self._parse_skill_metadata(value)
            else:
                metadata[key] = value

        return metadata

    # ==================== Security Scanning ====================

    def scan_skill(self, name: str) -> dict[str, Any] | None:
        """Scan a skill for security issues.

        Args:
            name: Skill name.

        Returns:
            Scan result dictionary or None if skill not found or scanner unavailable.
        """
        if not self._security_scanner:
            return None

        skill_dir = self._get_skill_dir(name)
        if not skill_dir:
            return None

        result = self._security_scanner.scan_skill(skill_dir.path, name)
        return {
            "skill_name": result.skill_name,
            "is_safe": result.is_safe,
            "max_severity": result.max_severity.value,
            "findings_count": len(result.findings),
            "findings": [
                {
                    "severity": f.severity.value,
                    "category": f.category.value,
                    "title": f.title,
                    "description": f.description,
                    "file_path": f.file_path,
                    "line_number": f.line_number,
                    "rule_id": f.rule_id,
                }
                for f in result.findings
            ],
            "scanned_files": result.scanned_files,
            "scan_duration_seconds": result.scan_duration_seconds,
        }

    def is_skill_safe(self, name: str) -> bool:
        """Quick check if a skill is safe to load.

        Args:
            name: Skill name.

        Returns:
            True if safe or scanner unavailable, False if issues found.
        """
        if not self._security_scanner:
            return True

        skill_dir = self._get_skill_dir(name)
        if not skill_dir:
            return False

        return self._security_scanner.quick_check(skill_dir.path)

    def install_skill(
        self,
        source: str | Path,
        name: str | None = None,
        skip_security_check: bool = False,
    ) -> dict[str, Any]:
        """Install a skill from a directory or zip file.

        Args:
            source: Path to skill directory or zip file.
            name: Optional name for the skill (defaults to directory name).
            skip_security_check: If True, skip security scanning.

        Returns:
            Installation result with 'success', 'name', 'path', 'warnings', 'scan_result'.
        """
        import shutil
        import tempfile
        import zipfile

        source_path = Path(source)
        result = {
            "success": False,
            "name": name or source_path.name,
            "path": None,
            "warnings": [],
            "scan_result": None,
        }

        # Handle zip files
        if source_path.suffix == ".zip":
            temp_dir = tempfile.mkdtemp()
            try:
                with zipfile.ZipFile(source_path, "r") as zf:
                    zf.extractall(temp_dir)
                # Find skill directory in extracted content
                skill_dirs = list(Path(temp_dir).glob("*/SKILL.md"))
                if skill_dirs:
                    source_path = skill_dirs[0].parent
                else:
                    result["warnings"].append("No SKILL.md found in zip file")
                    return result
            except zipfile.BadZipFile:
                result["warnings"].append("Invalid zip file")
                return result

        # Validate source
        if not source_path.is_dir():
            result["warnings"].append("Source is not a directory")
            return result

        skill_md = source_path / "SKILL.md"
        if not skill_md.exists():
            result["warnings"].append("SKILL.md not found in source directory")
            return result

        # Security scan
        if not skip_security_check and self._security_scanner:
            scan_result = self._security_scanner.scan_skill(source_path, result["name"])
            result["scan_result"] = {
                "is_safe": scan_result.is_safe,
                "max_severity": scan_result.max_severity.value,
                "findings_count": len(scan_result.findings),
            }

            if not scan_result.is_safe:
                result["warnings"].append(
                    f"Security scan failed with {scan_result.max_severity.value} severity issues"
                )
                return result

        # Install to workspace skills
        target_name = result["name"]
        target_path = self.workspace_skills / target_name

        # Ensure workspace skills directory exists
        self.workspace_skills.mkdir(parents=True, exist_ok=True)

        # Remove existing skill if present
        if target_path.exists():
            shutil.rmtree(target_path)

        # Copy skill directory
        shutil.copytree(source_path, target_path)

        result["success"] = True
        result["path"] = str(target_path)

        return result
