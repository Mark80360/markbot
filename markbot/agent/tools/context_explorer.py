"""Context explorer tools for AI-driven dynamic loading.

Provides three tools that allow the AI to autonomously decide
what context to load, similar to browsing a book's table of contents.

Tools:
- explore_context_catalog: View available context sources (table of contents)
- search_context: Search within specific context source (keyword search)
- load_context: Load full content from a specific entry (detailed reading)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TYPE_CHECKING

from loguru import logger

from markbot.agent.tools.base import Tool
from markbot.utils.constants import BOOTSTRAP_FILES
from markbot.utils.helpers import format_time

if TYPE_CHECKING:
    from ..memory.base import BaseMemoryManager


class ExploreContextCatalogTool(Tool):
    """View available context sources catalog.

    Returns a lightweight directory/table-of-contents for all available
    context sources (memory, bootstrap files, workspace info, etc.).
    This is like looking at a book's table of contents before reading.
    """

    @property
    def name(self) -> str:
        return "explore_context_catalog"

    @property
    def description(self) -> str:
        return (
            "Explore and view the catalog of all available context sources. "
            "Returns a lightweight table-of-contents showing what information "
            "is available to load. Use this FIRST when you need background "
            "context but don't know what's available. "
            "This is like checking a book's index before reading."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source_type": {
                    "type": "string",
                    "enum": ["all", "memory", "bootstrap", "workspace"],
                    "default": "all",
                    "description": (
                        "Type of context source to explore. "
                        "'all' shows everything, others filter by type."
                    ),
                },
            },
            "required": [],
        }

    def __init__(
        self,
        workspace: Path | None = None,
        memory_manager: "BaseMemoryManager | None" = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.workspace = workspace
        self._memory_manager = memory_manager

        self.BOOTSTRAP_FILES = BOOTSTRAP_FILES

    async def _legacy_execute(
        self,
        source_type: str = "all",
        **kwargs: Any,
    ) -> str:
        try:
            sections = []

            if source_type in ("all", "bootstrap"):
                bootstrap_section = self._build_bootstrap_catalog()
                if bootstrap_section:
                    sections.append(bootstrap_section)

            if source_type in ("all", "memory"):
                memory_section = self._build_memory_catalog()
                if memory_section:
                    sections.append(memory_section)

            if source_type in ("all", "workspace"):
                workspace_section = self._build_workspace_catalog()
                if workspace_section:
                    sections.append(workspace_section)

            if not sections:
                return "No context sources found in workspace."

            result = "# Available Context Catalog\n\n"
            result += "\n\n".join(sections)
            result += "\n\n---\n"
            result += "**Next Steps:**\n"
            result += "- Use `search_context` to find specific entries by keyword\n"
            result += "- Use `load_context` to load full content of a specific entry\n"

            return result

        except Exception as e:
            logger.error(f"Context catalog exploration failed: {e}")
            return f"Error exploring context catalog: {e}"

    def _build_bootstrap_catalog(self) -> str:
        """Build lightweight catalog for bootstrap files."""
        if not self.workspace or not self.workspace.exists():
            return ""

        lines = ["## Bootstrap Files (Static Configuration)\n"]
        lines.append("These configuration files define your identity, preferences, and guidelines.\n")

        has_files = False
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                has_files = True
                stat = file_path.stat()
                size_kb = stat.st_size / 1024

                lines.append(f"### {filename}")
                lines.append(f"- Size: {size_kb:.1f} KB")
                lines.append(f"- Last modified: {format_time(stat.st_mtime)}")

                first_line = self._get_first_line(file_path)
                if first_line:
                    lines.append(f"- Preview: {first_line[:100]}...")

                lines.append("")

        if not has_files:
            lines.append("*No bootstrap files found*\n")

        return "\n".join(lines)

    def _build_memory_catalog(self) -> str:
        """Build catalog for memory entries."""
        lines = ["## Memory Entries (Persistent Knowledge)\n"]
        lines.append("Contains user preferences, project history, decisions, and learned facts.\n")

        if not self._memory_manager:
            lines.append("*Memory manager not enabled*")
            return "\n".join(lines)

        try:
            if hasattr(self._memory_manager, 'list_memory_entries'):
                entries = self._memory_manager.list_memory_entries()

                if entries:
                    for idx, entry in enumerate(entries[:10], 1):
                        title = entry.get('title', 'Untitled')
                        source = entry.get('source', 'memory')
                        date = entry.get('date', '')
                        preview = entry.get('preview', '')[:80]

                        lines.append(f"{idx}. **{title}**")
                        lines.append(f"   - Source: {source}")
                        if date:
                            lines.append(f"   - Date: {date}")
                        lines.append(f"   - Preview: {preview}...")
                        lines.append("")
                else:
                    lines.append("*No memory entries found*")
            else:
                memory_file = self.workspace / "MEMORY.md"
                if memory_file.exists():
                    stat = memory_file.stat()
                    size_kb = stat.st_size / 1024
                    lines.append(f"- MEMORY.md ({size_kb:.1f} KB)")
                    lines.append("- Contains: User preferences, project notes, learned facts")
                    lines.append("")
                else:
                    lines.append("*No memory storage found*")

        except Exception as e:
            logger.warning(f"Failed to build memory catalog: {e}")
            lines.append(f"*Memory catalog unavailable: {e}*")

        return "\n".join(lines)

    def _build_workspace_catalog(self) -> str:
        """Build catalog for workspace context."""
        if not self.workspace or not self.workspace.exists():
            return ""

        lines = ["## Workspace Context (Project Information)\n"]

        git_dir = self.workspace / ".git"
        if git_dir.exists():
            lines.append("- Git repository detected")
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "log", "--oneline", "-5"],
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.stdout.strip():
                    lines.append("\n**Recent commits:**")
                    for line in result.stdout.strip().split('\n')[:5]:
                        lines.append(f"  - {line}")
            except Exception:
                pass

        skills_dir = self.workspace / "skills"
        if skills_dir.exists() and skills_dir.is_dir():
            skill_count = len([d for d in skills_dir.iterdir() if d.is_dir()])
            lines.append(f"\n- Custom skills: {skill_count} skill(s) available")

        sessions_dir = self.workspace / "sessions"
        if sessions_dir.exists() and sessions_dir.is_dir():
            session_count = len(list(sessions_dir.glob("*.json")))
            lines.append(f"- Session history: {session_count} session(s)")

        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _get_first_line(file_path: Path) -> str | None:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        return line
                    elif line.startswith('#'):
                        return line.lstrip('#').strip()
        except Exception:
            return None


class SearchContextTool(Tool):
    """Search within context sources.

    Searches across memory, bootstrap files, and other context sources
    for relevant information based on keywords or semantic query.
    Returns summarized results with IDs that can be used with load_context.
    """

    @property
    def name(self) -> str:
        return "search_context"

    @property
    def description(self) -> str:
        return (
            "Search for specific context by keyword or semantic query. "
            "Use after exploring the catalog to find relevant entries. "
            "Returns matching results with IDs that you can use with "
            "`load_context` to get full content."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query - can be keywords or natural language. "
                        "Examples: 'project architecture', 'user preferences', 'bug fix'"
                    ),
                },
                "source": {
                    "type": "string",
                    "enum": ["all", "memory", "bootstrap", "workspace"],
                    "default": "all",
                    "description": "Which context source to search",
                },
                "max_results": {
                    "type": "integer",
                    "default": 5,
                    "description": "Maximum number of results (default: 5)",
                },
            },
            "required": ["query"],
        }

    def __init__(
        self,
        workspace: Path | None = None,
        memory_manager: "BaseMemoryManager | None" = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.workspace = workspace
        self._memory_manager = memory_manager
        self.BOOTSTRAP_FILES = BOOTSTRAP_FILES

    async def _legacy_execute(
        self,
        query: str,
        source: str = "all",
        max_results: int = 5,
        **kwargs: Any,
    ) -> str:
        try:
            all_results = []

            if source in ("all", "memory"):
                memory_results = await self._search_memory(query, max_results)
                all_results.extend(memory_results)

            if source in ("all", "bootstrap"):
                bootstrap_results = self._search_bootstrap(query, max_results)
                all_results.extend(bootstrap_results)

            if len(all_results) > max_results:
                all_results = all_results[:max_results]

            if not all_results:
                return f"No results found for query: '{query}'"

            formatted = [f"# Search Results: '{query}'\n"]
            formatted.append(f"Found {len(all_results)} result(s):\n")

            for idx, result in enumerate(all_results, 1):
                formatted.append(f"### {idx}. [{result['source']}] {result['title']}")
                formatted.append(f"- **ID:** `{result['id']}`")
                if result.get('score'):
                    formatted.append(f"- **Relevance:** {result['score']}")
                formatted.append(f"- **Preview:** {result['preview'][:150]}...")
                formatted.append("")

            formatted.append("---")
            formatted.append("\n**To load full content:**")
            formatted.append(f"Use `load_context(source=\"{all_results[0]['source']}\", context_id=\"{all_results[0]['id']}\")`")

            return "\n".join(formatted)

        except Exception as e:
            logger.error(f"Context search failed: {e}")
            return f"Error searching context: {e}"

    async def _search_memory(self, query: str, max_results: int) -> list[dict]:
        """Search memory using semantic search."""
        results = []

        if not self._memory_manager:
            return results

        try:
            if hasattr(self._memory_manager, 'memory_search'):
                search_results = await self._memory_manager.memory_search(
                    query=query,
                    max_results=max_results,
                    min_score=0.1,
                )

                for idx, r in enumerate(search_results):
                    results.append({
                        'id': f'mem_{idx}',
                        'source': 'memory',
                        'title': r.get('source', r.get('file', 'Memory Entry')),
                        'preview': r.get('content', ''),
                        'score': r.get('score'),
                        'full_content': r.get('content', ''),
                    })
            else:
                memory_file = self.workspace / "MEMORY.md"
                if memory_file.exists():
                    content = memory_file.read_text(encoding='utf-8')
                    if query.lower() in content.lower():
                        idx = content.lower().find(query.lower())
                        start = max(0, idx - 50)
                        end = min(len(content), idx + len(query) + 200)
                        preview = content[start:end]

                        results.append({
                            'id': 'mem_0',
                            'source': 'memory',
                            'title': 'MEMORY.md',
                            'preview': preview,
                            'score': None,
                            'full_content': content,
                        })

        except Exception as e:
            logger.warning(f"Memory search failed: {e}")

        return results

    def _search_bootstrap(self, query: str, max_results: int) -> list[dict]:
        """Search bootstrap files for keyword matches."""
        results = []

        if not self.workspace or not self.workspace.exists():
            return results

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if not file_path.exists():
                continue

            try:
                content = file_path.read_text(encoding='utf-8')

                if query.lower() in content.lower():
                    idx = content.lower().find(query.lower())
                    start = max(0, idx - 50)
                    end = min(len(content), idx + len(query) + 200)
                    preview = content[start:end]

                    results.append({
                        'id': filename.replace('.', '_'),
                        'source': 'bootstrap',
                        'title': filename,
                        'preview': preview,
                        'score': None,
                        'full_content': content,
                    })

                    if len(results) >= max_results:
                        break

            except Exception as e:
                logger.warning(f"Bootstrap search failed for {filename}: {e}")

        return results


class LoadContextTool(Tool):
    """Load full content from a specific context entry.

    Loads the complete content of a specific context entry identified
    by its ID from search_context results.
    This is like opening a book to read a specific chapter in detail.
    """

    @property
    def name(self) -> str:
        return "load_context"

    @property
    def description(self) -> str:
        return (
            "Load the full content of a specific context entry. "
            "Use the ID returned by `search_context` to specify which entry to load. "
            "This loads the complete content, so use it only for entries "
            "you've confirmed are relevant from search results."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "context_id": {
                    "type": "string",
                    "description": (
                        "ID of the context entry to load (from search_context results). "
                        "Example: 'mem_0', 'AGENTS_md', 'SOUL_md'"
                    ),
                },
                "max_tokens": {
                    "type": "integer",
                    "default": 4000,
                    "description": (
                        "Maximum tokens to load (default: 4000). "
                        "Use smaller values for quick overviews, larger for detailed reading."
                    ),
                },
            },
            "required": ["context_id"],
        }

    def __init__(
        self,
        workspace: Path | None = None,
        memory_manager: "BaseMemoryManager | None" = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.workspace = workspace
        self._memory_manager = memory_manager
        self._search_cache: dict[str, dict] = {}

    async def _legacy_execute(
        self,
        context_id: str,
        max_tokens: int = 4000,
        **kwargs: Any,
    ) -> str:
        try:
            content = None
            source_type = "unknown"

            if context_id.startswith('mem_'):
                content, source_type = await self._load_memory(context_id)
            else:
                content, source_type = self._load_bootstrap(context_id)

            if content is None:
                return f"Error: Context entry '{context_id}' not found or unavailable."

            tokens_estimated = len(content) // 4

            if tokens_estimated > max_tokens:
                truncated = content[:max_tokens * 4]
                content = truncated + f"\n\n... [truncated at ~{max_tokens} tokens]"

            formatted = [
                f"# Loaded Context: {context_id}",
                f"**Source:** {source_type}\n",
                content,
                "\n---",
                f"*Loaded ~{min(tokens_estimated, max_tokens)} tokens*",
            ]

            return "\n".join(formatted)

        except Exception as e:
            logger.error(f"Context loading failed: {e}")
            return f"Error loading context '{context_id}': {e}"

    async def _load_memory(self, context_id: str) -> tuple[str | None, str]:
        """Load memory entry."""
        if not self._memory_manager:
            return None, "memory"

        try:
            if hasattr(self._memory_manager, 'list_memory_entries'):
                entries = self._memory_manager.list_memory_entries()
                idx = int(context_id.split('_')[1]) if '_' in context_id else 0

                if idx < len(entries):
                    entry = entries[idx]
                    return entry.get('content', ''), 'memory'
        except Exception as e:
            logger.warning(f"Failed to load memory {context_id}: {e}")

        memory_file = self.workspace / "MEMORY.md"
        if memory_file.exists():
            return memory_file.read_text(encoding='utf-8'), 'memory'

        return None, "memory"

    def _load_bootstrap(self, context_id: str) -> tuple[str | None, str]:
        """Load bootstrap file with duplicate detection.

        Detects if MEMORY.md is being reloaded and returns a hint instead
        of duplicating the content that's already in the system prompt.
        """
        if not self.workspace or not self.workspace.exists():
            return None, "bootstrap"

        filename_map = {
            'AGENTS_md': 'AGENTS.md',
            'SOUL_md': 'SOUL.md',
            'USER_md': 'USER.md',
            'TOOLS_md': 'TOOLS.md',
            'MEMORY_md': 'MEMORY.md',
            'PROFILE_md': 'PROFILE.md',
        }

        filename = filename_map.get(context_id, context_id.replace('_', '.'))

        if filename == 'MEMORY.md':
            hint = (
                "*Note: MEMORY.md content is already included in the system prompt above. "
                "Reloading it here would be redundant. "
                "If you need more details, consider using `search_context` to find specific sections.*"
            )
            return hint, 'bootstrap'

        file_path = self.workspace / filename

        if file_path.exists():
            try:
                return file_path.read_text(encoding='utf-8'), 'bootstrap'
            except Exception as e:
                logger.warning(f"Failed to load bootstrap {filename}: {e}")

        return None, 'bootstrap'
