"""File and content search tools: Glob and Grep.

Enhanced with:
- Directory exclusion (.git, node_modules, __pycache__, etc.)
- Binary file detection and skipping
- Word boundary matching
- File-name-only mode
- Match count mode
- Recursive depth control
- Richer output formatting
"""

import fnmatch
import re
from pathlib import Path
from typing import Any

from markbot.tools.base import Tool, _resolve_path
from markbot.utils.constants import (
    BINARY_EXTENSIONS as _BINARY_EXTENSIONS,
)
from markbot.utils.constants import (
    IGNORE_DIRS as _DEFAULT_IGNORE_DIRS,
)
from markbot.utils.constants import (
    MAX_CONTEXT_LINES,
    MAX_SEARCH_RESULTS,
)
from markbot.utils.helpers import format_time


def _is_binary_file(file_path: Path) -> bool:
    """Check if a file is binary by extension or content sniffing."""
    if file_path.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(8192)
            if b'\x00' in chunk:
                return True
    except (OSError, PermissionError):
        pass
    return False


def _should_ignore_dir(dir_name: str) -> bool:
    """Check if directory should be ignored."""
    if dir_name.startswith('.') and dir_name not in {".github", ".gitlab", ".circleci"}:
        return True
    if dir_name in _DEFAULT_IGNORE_DIRS:
        return True
    return False


def _collect_files(
    root: Path,
    include_pattern: str | None = None,
    exclude_pattern: str | None = None,
    max_depth: int | None = None,
) -> list[Path]:
    """Collect files with filtering and depth control."""
    files = []

    def walk(current: Path, depth: int):
        if max_depth is not None and depth > max_depth:
            return
        try:
            entries = sorted(current.iterdir())
        except (PermissionError, OSError):
            return

        for entry in entries:
            if entry.is_dir():
                if _should_ignore_dir(entry.name):
                    continue
                if exclude_pattern and fnmatch.fnmatch(entry.name, exclude_pattern):
                    continue
                walk(entry, depth + 1)
            elif entry.is_file():
                rel = entry.relative_to(root)
                rel_str = str(rel).replace("\\", "/")

                if include_pattern:
                    if not (fnmatch.fnmatch(rel_str, include_pattern) or
                            fnmatch.fnmatch(entry.name, include_pattern)):
                        continue
                if exclude_pattern:
                    if fnmatch.fnmatch(rel_str, exclude_pattern) or \
                       fnmatch.fnmatch(entry.name, exclude_pattern):
                        continue
                files.append(entry)

    walk(root, 0)
    return files


class GlobTool(Tool):
    """Enhanced tool to search for files using glob patterns."""

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
        return (
            "Search for files matching a glob pattern (e.g., '**/*.py', 'src/**/*.ts'). "
            "Automatically ignores VCS directories (.git), dependencies (node_modules), "
            "build artifacts (dist, __pycache__), and caches. "
            "Supports sorting, size filtering, and detailed output."
        )

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
                },
                "exclude": {
                    "type": "string",
                    "description": "Glob pattern to exclude (e.g., '*.test.js', '__pycache__/*')"
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["modified", "name", "size"],
                    "description": "Sort results by: modified (default), name, or size",
                    "default": "modified"
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum number of results (default: 100, max: {MAX_SEARCH_RESULTS})",
                    "minimum": 1,
                    "maximum": MAX_SEARCH_RESULTS,
                    "default": 100
                },
                "show_details": {
                    "type": "boolean",
                    "description": "Show file details (size, modified time) alongside paths",
                    "default": False
                }
            },
            "required": ["pattern"]
        }

    async def _legacy_execute(self, **kwargs: Any) -> str:
        pattern = kwargs.get("pattern", "")
        path = kwargs.get("path")
        exclude = kwargs.get("exclude")
        sort_by = kwargs.get("sort_by", "modified")
        max_results = min(kwargs.get("max_results", 100), MAX_SEARCH_RESULTS)
        show_details = kwargs.get("show_details", False)

        try:
            search_dir = _resolve_path(path or ".", self._workspace, self._allowed_dir)
            if not search_dir.exists():
                return f"Error: Directory not found: {path or '.'}"
            if not search_dir.is_dir():
                return f"Error: Not a directory: {path or '.'}"

            matches = []
            for file_path in _collect_files(search_dir, include_pattern=pattern, exclude_pattern=exclude):
                rel_path = file_path.relative_to(search_dir)
                if fnmatch.fnmatch(str(rel_path).replace("\\", "/"), pattern) or \
                   fnmatch.fnmatch(str(rel_path), pattern):
                    matches.append(file_path)

            if not matches:
                excluded_hint = f" (excluded: {exclude})" if exclude else ""
                return f"No files found matching pattern: '{pattern}'{excluded_hint}"

            # Sort
            if sort_by == "name":
                matches.sort(key=lambda p: str(p.relative_to(search_dir)).lower())
            elif sort_by == "size":
                matches.sort(key=lambda p: p.stat().st_size, reverse=True)
            else:  # modified
                matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)

            # Format output
            total = len(matches)
            truncated = total > max_results
            matches = matches[:max_results]

            lines = []
            for fp in matches:
                if show_details:
                    stat = fp.stat()
                    size_str = _format_size(stat.st_size)
                    mtime_str = format_time(stat.st_mtime)
                    rel = fp.relative_to(search_dir)
                    lines.append(f"{size_str:>10}  {mtime_str}  {rel}")
                else:
                    lines.append(str(fp))

            result = "\n".join(lines)
            if truncated:
                result += f"\n\n... ({total - max_results} more files not shown)"

            result += f"\n\nTotal: {total} file(s)"
            return result

        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error searching files: {str(e)}"


class GrepTool(Tool):
    """Enhanced tool to search file contents using regex patterns."""

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
        return (
            "Search file contents using regex patterns. Returns matching lines with file paths and line numbers. "
            "Automatically skips binary files, VCS directories (.git), and build artifacts. "
            "Supports word-boundary matching, file-name-only mode, match counting, and context lines."
        )

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
                    "description": "Glob pattern to include only certain files (e.g., '*.py', '*.ts')"
                },
                "exclude": {
                    "type": "string",
                    "description": "Glob pattern to exclude files (e.g., '*.test.js', '*.min.*')"
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default: false)",
                    "default": False
                },
                "word_match": {
                    "type": "boolean",
                    "description": "Match whole words only (adds \\b boundaries)",
                    "default": False
                },
                "context_lines": {
                    "type": "integer",
                    "description": f"Number of context lines around matches (0-{MAX_CONTEXT_LINES}, default: 0)",
                    "minimum": 0,
                    "maximum": MAX_CONTEXT_LINES,
                    "default": 0
                },
                "mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        "Output mode:\n"
                        "- content: Show matching lines with context (default)\n"
                        "- files_with_matches: List only filenames that contain matches\n"
                        "- count: Show match count per file"
                    ),
                    "default": "content"
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum directory recursion depth (default: unlimited)",
                    "minimum": 1,
                }
            },
            "required": ["pattern"]
        }

    async def _legacy_execute(self, **kwargs: Any) -> str:
        pattern = kwargs.get("pattern", "")
        path = kwargs.get("path")
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        case_insensitive = kwargs.get("case_insensitive", False)
        word_match = kwargs.get("word_match", False)
        context_lines = min(max(kwargs.get("context_lines", 0), 0), 20)
        mode = kwargs.get("mode", "content")
        max_depth = kwargs.get("max_depth")

        try:
            search_path = _resolve_path(path or ".", self._workspace, self._allowed_dir)
            if not search_path.exists():
                return f"Error: Path not found: {path or '.'}"

            flags = re.IGNORECASE if case_insensitive else 0
            try:
                if word_match:
                    pattern = r'\b' + re.escape(pattern) + r'\b'
                regex = re.compile(pattern, flags)
            except re.error as e:
                return f"Error: Invalid regex pattern: {e}"

            # Collect files to search
            if search_path.is_file():
                files = [search_path]
            else:
                files = _collect_files(search_path, include_pattern=include,
                                       exclude_pattern=exclude, max_depth=max_depth)

            # Search based on mode
            if mode == "files_with_matches":
                return self._search_files_with_matches(regex, files, pattern)
            elif mode == "count":
                return self._search_count(regex, files, pattern)
            else:
                return self._search_content(regex, files, pattern, context_lines)

        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error searching content: {str(e)}"

    def _search_content(self, regex, files: list[Path], pattern: str, context_lines: int) -> str:
        """Search and return matching lines with optional context."""
        matches = []
        files_searched = 0
        files_skipped_binary = 0
        max_matches = 200

        for file_path in files:
            try:
                if _is_binary_file(file_path):
                    files_skipped_binary += 1
                    continue

                files_searched += 1
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                lines = content.splitlines()

                file_matches = []
                for i, line in enumerate(lines, 1):
                    if regex.search(line):
                        if context_lines > 0:
                            start = max(0, i - context_lines - 1)
                            end = min(len(lines), i + context_lines)
                            ctx_lines = []
                            for j in range(start, end):
                                prefix = ">" if j == i - 1 else "-"
                                ctx_lines.append(f"{prefix} {j + 1}| {lines[j]}")
                            file_matches.append(f"\n{file_path.relative_to(file_path.parent.parent if file_path.parent.parent != file_path.parents[0] else Path('.'))}:\n" + "\n".join(ctx_lines))
                        else:
                            rel_path = file_path
                            try:
                                rel_path = file_path.relative_to(Path.cwd())
                            except ValueError:
                                pass
                            file_matches.append(f"{rel_path}:{i}: {line}")

                        if len(matches) + len(file_matches) >= max_matches:
                            matches.extend(file_matches)
                            break

                matches.extend(file_matches)
                if len(matches) >= max_matches:
                    break

            except (UnicodeDecodeError, PermissionError, OSError):
                continue

        if not matches:
            extra_info = ""
            if files_skipped_binary > 0:
                extra_info = f" (skipped {files_skipped_binary} binary files)"
            return f"No matches found for pattern: '{pattern}'{extra_info} (searched {files_searched} text files)"

        result = "\n".join(matches)
        summary_parts = [f"Pattern: '{pattern}'", f"Files searched: {files_searched}"]
        if files_skipped_binary > 0:
            summary_parts.append(f"Binary skipped: {files_skipped_binary}")

        if len(matches) >= max_matches:
            result += f"\n\n... (limit reached: showing first {max_matches} of {len(matches)} matches)"
        result += f"\n\n{' | '.join(summary_parts)}"

        return result

    def _search_files_with_matches(self, regex, files: list[Path], pattern: str) -> str:
        """Return only filenames that contain at least one match."""
        matched_files = []

        for file_path in files:
            try:
                if _is_binary_file(file_path):
                    continue

                content = file_path.read_text(encoding="utf-8", errors="ignore")
                if regex.search(content):
                    matched_files.append(str(file_path))

            except (UnicodeDecodeError, PermissionError, OSError):
                continue

        if not matched_files:
            return f"No files contain pattern: '{pattern}'"

        return "\n".join(matched_files) + f"\n\n({len(matched_files)} file(s) matched)"

    def _search_count(self, regex, files: list[Path], pattern: str) -> str:
        """Return match count per file."""
        counts = []
        total_matches = 0
        files_searched = 0

        for file_path in files:
            try:
                if _is_binary_file(file_path):
                    continue

                files_searched += 1
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                count = len(regex.findall(content))
                if count > 0:
                    counts.append((file_path, count))
                    total_matches += count

            except (UnicodeDecodeError, PermissionError, OSError):
                continue

        if not counts:
            return f"No matches found for: '{pattern}' (searched {files_searched} files)"

        lines = [f"{fp}: {count}" for fp, count in counts]
        result = "\n".join(lines)
        result += f"\n\nTotal: {total_matches} match(es) in {len(counts)} file(s)"
        return result


def _format_size(size_bytes: int) -> str:
    """Format file size in human-readable form."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f}TB"

