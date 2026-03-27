"""File and content search tools: Glob and Grep."""

import fnmatch
import re
from pathlib import Path
from typing import Any

from markbot.agent.tools.base import Tool


def _resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
) -> Path:
    """Resolve path against workspace (if relative) and enforce directory restriction."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if allowed_dir:
        try:
            resolved.relative_to(allowed_dir.resolve())
        except ValueError:
            raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


class GlobTool(Tool):
    """Tool to search for files using glob patterns."""

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
    ):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return "Search for files matching a glob pattern (e.g., '**/*.py', 'src/**/*.ts'). Returns file paths sorted by modification time (most recent first)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files (e.g., '**/*.py', 'src/**/*.ts')"
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: workspace root)"
                }
            },
            "required": ["pattern"]
        }

    async def execute(self, **kwargs: Any) -> str:
        pattern = kwargs.get("pattern", "")
        path = kwargs.get("path")
        
        try:
            search_dir = _resolve_path(path or ".", self._workspace, self._allowed_dir)
            if not search_dir.exists():
                return f"Error: Directory not found: {path or '.'}"
            if not search_dir.is_dir():
                return f"Error: Not a directory: {path or '.'}"

            matches = []
            for file_path in search_dir.rglob("*"):
                if file_path.is_file():
                    rel_path = file_path.relative_to(search_dir)
                    # Try both native path and forward-slash variant
                    if fnmatch.fnmatch(str(rel_path), pattern) or fnmatch.fnmatch(str(rel_path).replace("\\", "/"), pattern):
                        matches.append(str(file_path))

            if not matches:
                return f"No files found matching pattern: {pattern}"

            # Sort by modification time (most recent first)
            matches.sort(key=lambda p: Path(p).stat().st_mtime, reverse=True)

            # Limit results
            max_results = 100
            if len(matches) > max_results:
                return "\n".join(matches[:max_results]) + f"\n\n... ({len(matches) - max_results} more files not shown)"

            return "\n".join(matches)

        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error searching files: {str(e)}"


class GrepTool(Tool):
    """Tool to search file contents using regex patterns."""

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
    ):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return "Search file contents for a pattern using regex. Returns matching lines with file paths and line numbers."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for"
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file to search in (default: workspace root)"
                },
                "include": {
                    "type": "string",
                    "description": "Optional glob pattern to filter files (e.g., '*.py')"
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default: false)"
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Number of context lines to show around matches (default: 0)",
                    "minimum": 0,
                    "maximum": 5
                }
            },
            "required": ["pattern"]
        }

    async def execute(self, **kwargs: Any) -> str:
        pattern = kwargs.get("pattern", "")
        path = kwargs.get("path")
        include = kwargs.get("include")
        case_insensitive = kwargs.get("case_insensitive", False)
        context_lines = kwargs.get("context_lines", 0)
        
        try:
            search_path = _resolve_path(path or ".", self._workspace, self._allowed_dir)
            if not search_path.exists():
                return f"Error: Path not found: {path or '.'}"

            flags = re.IGNORECASE if case_insensitive else 0
            try:
                regex = re.compile(pattern, flags)
            except re.error as e:
                return f"Error: Invalid regex pattern: {e}"

            matches = []
            files_searched = 0
            max_matches = 100

            # Determine files to search
            if search_path.is_file():
                files = [search_path]
            else:
                files = []
                for file_path in search_path.rglob("*"):
                    if file_path.is_file():
                        if include:
                            rel_path = file_path.relative_to(search_path)
                            if not (fnmatch.fnmatch(str(rel_path), include) or
                                   fnmatch.fnmatch(str(rel_path).replace("\\", "/"), include)):
                                continue
                        files.append(file_path)

            # Search files
            for file_path in files:
                try:
                    files_searched += 1
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    lines = content.splitlines()

                    for i, line in enumerate(lines, 1):
                        if regex.search(line):
                            if context_lines > 0:
                                start = max(0, i - context_lines - 1)
                                end = min(len(lines), i + context_lines)
                                context = []
                                for j in range(start, end):
                                    prefix = ">" if j == i - 1 else " "
                                    context.append(f"{prefix} {j + 1}: {lines[j]}")
                                matches.append(f"{file_path}:\n" + "\n".join(context))
                            else:
                                matches.append(f"{file_path}:{i}: {line}")

                            if len(matches) >= max_matches:
                                break

                    if len(matches) >= max_matches:
                        break

                except (UnicodeDecodeError, PermissionError):
                    continue

            if not matches:
                return f"No matches found for pattern: {pattern} (searched {files_searched} files)"

            result = "\n".join(matches)
            if len(matches) >= max_matches:
                result += f"\n\n... (limit reached, {files_searched} files searched)"

            return result

        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error searching content: {str(e)}"
