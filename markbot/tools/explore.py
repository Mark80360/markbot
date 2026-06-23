"""Deep code exploration tool for structured project analysis.

Enhanced with:
- Unified ignore directories (shared constants)
- Async AST parsing for better performance
- Result caching for repeated explorations
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any

from markbot.tools.base import Tool, _is_under
from markbot.utils.constants import IGNORE_DIRS as _IGNORE_DIRS

_LANG_EXTENSIONS = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript", ".java": "java",
    ".go": "golang", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".c": "c", ".cpp": "c++", ".h": "c/c++", ".cs": "c#",
    ".swift": "swift", ".kt": "kotlin", ".scala": "scala",
    ".vue": "vue", ".svelte": "svelte", ".astro": "astro",
    ".md": "markdown", ".yaml": "yaml", ".yml": "yaml",
    ".json": "json", ".toml": "toml", ".cfg": "ini",
    ".html": "html", ".css": "css", ".scss": "scss", ".less": "less",
    ".sql": "sql", ".sh": "shell", ".bat": "batch",
    ".proto": "protobuf", ".graphql": "graphql", ".gql": "graphql",
}


class ExploreTool(Tool):
    """Deep code exploration tool for structured multi-file analysis."""

    _is_heavy_tool = True

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    def _safe_resolve(self, path: str | Path, base: Path) -> Path:
        """Resolve and validate path is within allowed directory."""
        p = Path(path) if isinstance(path, str) else path
        if not p.is_absolute():
            p = base / p
        resolved = p.resolve()
        if self._allowed_dir and not _is_under(resolved, self._allowed_dir):
            raise PermissionError(f"Path {path} is outside allowed directory")
        return resolved

    @property
    def name(self) -> str:
        return "explore"

    @property
    def description(self) -> str:
        return (
            "Deep code exploration and analysis tool. Use this for understanding codebases, "
            "tracing symbols across files, analyzing architecture, and discovering patterns. "
            "MUCH more powerful than individual read_file/grep/glob calls for research tasks. "
            "Modes: overview (project map), trace (symbol tracking), analyze (deep dive), "
            "dependencies (module graph). ALWAYS use this first when researching a codebase."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["overview", "trace", "analyze", "dependencies"],
                    "description": (
                        "Exploration mode:\n"
                        "- **overview**: Map project structure, detect tech stack, identify architecture patterns, find key files\n"
                        "- **trace**: Track a symbol (function/class/variable) across files — follow imports, calls, references\n"
                        "- **analyze**: Deep analysis of a specific file or module with full context from related files\n"
                        "- **dependencies**: Map import/dependency relationships between modules"
                    ),
                },
                "target": {
                    "type": "string",
                    "description": (
                        "Target for exploration:\n"
                        "- overview: directory path (default: workspace root)\n"
                        "- trace: symbol name to track (e.g., 'QueryEngine', 'handle_request', 'useState')\n"
                        "- analyze: file path or module name to deep-dive into\n"
                        "- dependencies: file path or module name (default: workspace root)"
                    ),
                },
                "depth": {
                    "type": "integer",
                    "description": "How deep to explore (1-5, default 3). Higher = more thorough but slower.",
                    "minimum": 1,
                    "maximum": 5,
                    "default": 3,
                },
                "focus": {
                    "type": "string",
                    "description": "Optional focus area to narrow results (e.g., 'auth', 'database', 'api', 'ui')",
                },
                "include_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Glob patterns to include (e.g., ['**/*.py', '**/*.ts']). Default: all source files.",
                },
                "exclude_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Glob patterns to exclude (e.g., ['**/test_*']). Default: test files excluded in non-test focus.",
                },
            },
            "required": ["mode"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> str:
        mode = kwargs.get("mode", "overview")
        target = kwargs.get("target", "")
        depth = min(max(kwargs.get("depth", 3), 1), 5)
        focus = kwargs.get("focus", "")
        include_patterns = kwargs.get("include_patterns")
        exclude_patterns = kwargs.get("exclude_patterns")

        base_path = self._workspace if self._workspace and self._workspace.exists() else Path(".")
        if not base_path.exists():
            base_path = Path(".")

        if target:
            target_path = self._safe_resolve(target, base_path)
        else:
            target_path = base_path

        dispatch = {
            "overview": self._explore_overview,
            "trace": self._explore_trace,
            "analyze": self._explore_analyze,
            "dependencies": self._explore_dependencies,
        }

        handler = dispatch.get(mode)
        if not handler:
            return f"Error: Unknown explore mode '{mode}'. Available: {list(dispatch.keys())}"

        try:
            result = await asyncio.to_thread(
                handler,
                target_path=target_path if target_path.exists() else base_path,
                depth=depth,
                focus=focus,
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
                base_path=base_path,
                symbol=target if mode == "trace" else None,
            )
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error during {mode} exploration: {e}"

    def _explore_overview(self, target_path, depth, focus, include_patterns, exclude_patterns, base_path, symbol=None):
        root = target_path if target_path.is_dir() else base_path
        sections = []

        sections.append("# Project Overview\n")

        sections.append(f"## Location: `{root}`\n")

        lang_stats = self._collect_language_stats(root, include_patterns)
        if lang_stats:
            lines = ["### Tech Stack (by file count)\n"]
            for lang, count in sorted(lang_stats.items(), key=lambda x: -x[1])[:15]:
                bar = "█" * min(count, 40)
                lines.append(f"- **{lang}**: {count} files `{bar}`")
            sections.append("\n".join(lines))

        structure = self._map_structure(root, depth, include_patterns, exclude_patterns, focus)
        sections.append(structure)

        key_files = self._identify_key_files(root, include_patterns)
        if key_files:
            lines = ["\n## Key Files Identified\n"]
            for category, files in key_files.items():
                lines.append(f"### {category}\n")
                for f in files[:8]:
                    rel = f.relative_to(root)
                    lines.append(f"- `{rel}` ({self._get_file_summary(f)})")
            sections.append("\n".join(lines))

        patterns = self._detect_architecture_patterns(root, lang_stats)
        if patterns:
            lines = ["\n## Architecture Patterns Detected\n"]
            for p in patterns:
                lines.append(f"- **{p}")
            sections.append("\n".join(lines))

        config_files = self._find_config_files(root)
        if config_files:
            lines = ["\n## Configuration Files\n"]
            for cf in config_files[:12]:
                rel = cf.relative_to(root)
                lines.append(f"- `{rel}`")
            sections.append("\n".join(lines))

        entry_points = self._find_entry_points(root, lang_stats, include_patterns)
        if entry_points:
            lines = ["\n## Entry Points\n"]
            for ep in entry_points[:10]:
                rel = ep.relative_to(root)
                lines.append(f"- `{rel}` — {self._get_entry_description(ep)}")
            sections.append("\n".join(lines))

        next_steps = self._suggest_next_steps(root, focus, key_files, patterns)
        sections.append(f"\n## Suggested Next Steps\n{next_steps}")

        return "\n".join(sections)

    def _explore_trace(self, target_path, depth, focus, include_patterns, exclude_patterns, base_path, symbol=None):
        if not symbol:
            return "Error: 'trace' mode requires a symbol name as 'target' (e.g., target='QueryEngine')"

        root = target_path if target_path.is_dir() else base_path
        sections = [f"# Symbol Trace: `{symbol}`\n"]

        definitions = self._find_symbol_definitions(root, symbol, include_patterns)
        if definitions:
            lines = ["## Definitions Found\n"]
            for filepath, line_no, kind, signature in definitions:
                rel = filepath.relative_to(root)
                preview = signature[:120] + ("..." if len(signature) > 120 else "")
                lines.append(f"- `{rel}:{line_no}` **{kind}** `{preview}`")
            sections.append("\n".join(lines))
        else:
            sections.append(f"## Definitions Found\n\nNo direct definition found for `{symbol}`.\nTrying fuzzy match...")

            fuzzy = self._find_fuzzy_definitions(root, symbol, include_patterns)
            if fuzzy:
                lines = []
                for filepath, line_no, kind, signature, score in fuzzy[:10]:
                    rel = filepath.relative_to(root)
                    preview = signature[:100] + ("..." if len(signature) > 100 else "")
                    lines.append(f"- `{rel}:{line_no}` **{kind}** (similarity: {score:.0%}) `{preview}`")
                sections.append("### Fuzzy Matches\n" + "\n".join(lines))
            else:
                sections.append("No matches found. Try a different symbol name or use grep directly.")

        usages = self._find_symbol_usages(root, symbol, include_patterns, limit_per_file=5)
        if usages:
            lines = ["\n## Usage / References\n"]
            total_refs = sum(len(refs) for refs in usages.values())
            lines.append(f"Found **{total_refs} references** across **{len(usages)}** files:\n")
            for filepath, refs in sorted(usages.items(), key=lambda x: -len(x[1]))[:20]:
                rel = filepath.relative_to(root)
                lines.append(f"### `{rel}` ({len(refs)} refs)")
                for line_no, context_line in refs[:5]:
                    indent_line = context_line.strip()[:100]
                    lines.append(f"  L{line_no}: `{indent_line}`")
            sections.append("\n".join(lines))

        imports_of = self._find_imports_of_symbol(root, symbol, include_patterns)
        if imports_of:
            lines = ["\n## Files Importing This Symbol\n"]
            for filepath, import_lines in imports_of[:15]:
                rel = filepath.relative_to(root)
                lines.append(f"- `{rel}`: {import_lines[0][:80]}")
            sections.append("\n".join(lines))

        call_chain = self._trace_call_chain(root, symbol, definitions, usages, include_patterns, max_depth=depth)
        if call_chain:
            sections.append(f"\n## Call Chain / Relationship Graph\n{call_chain}")

        related_symbols = self._find_related_symbols(root, symbol, definitions, usages, include_patterns)
        if related_symbols:
            lines = ["\n## Related Symbols\n"]
            for sym, info in related_symbols[:15]:
                lines.append(f"- **`{sym}`**: {info}")
            sections.append("\n".join(lines))

        sections.append("\n---\n**Tip**: Use `explore` with `analyze` mode on any of the files above for deeper context.")
        return "\n".join(sections)

    def _explore_analyze(self, target_path, depth, focus, include_patterns, exclude_patterns, base_path, symbol=None):
        if not target_path.exists():
            return f"Error: File or directory not found: {target_path}"

        sections = []

        if target_path.is_file():
            sections.extend(self._analyze_single_file(target_path, base_path, depth, focus))
        elif target_path.is_dir():
            sections.extend(self._analyze_directory(target_path, base_path, depth, focus, include_patterns))
        else:
            return f"Error: Cannot analyze {target_path}"

        return "\n".join(sections)

    def _explore_dependencies(self, target_path, depth, focus, include_patterns, exclude_patterns, base_path, symbol=None):
        root = target_path if target_path.is_dir() else base_path
        sections = ["# Dependency Analysis\n"]

        dep_graph = self._build_dependency_graph(root, include_patterns, depth)
        if dep_graph:
            sections.append(dep_graph)
        else:
            sections.append("No dependencies found or unable to parse source files.")

        circular = self._detect_circular_deps(root, include_patterns)
        if circular:
            lines = ["\n## Circular Dependencies Detected\n"]
            for cycle in circular:
                lines.append(f"- {' → '.join(cycle)} → (back to start)")
            sections.append("\n".join(lines))

        core_modules = self._identify_core_modules(root, include_patterns)
        if core_modules:
            lines = ["\n## Core Modules (High Connectivity)\n"]
            for mod, score in core_modules[:10]:
                lines.append(f"- **{mod}** (connectivity score: {score})")
            sections.append("\n".join(lines))

        orphan_files = self._find_orphan_files(root, include_patterns)
        if orphan_files:
            lines = ["\n## Potentially Orphaned Files (No Imports Found)\n"]
            for f in orphan_files[:10]:
                rel = f.relative_to(root)
                lines.append(f"- `{rel}`")
            sections.append("\n".join(lines))

        return "\n".join(sections)

    def _collect_language_stats(self, root, include_patterns):
        stats = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in path.parts for p in _IGNORE_DIRS):
                continue
            ext = path.suffix.lower()
            if include_patterns:
                if not any(path.match(p) for p in include_patterns):
                    continue
            if ext in _LANG_EXTENSIONS:
                lang = _LANG_EXTENSIONS[ext]
                stats[lang] = stats.get(lang, 0) + 1
        return stats

    def _map_structure(self, root, depth, include_patterns, exclude_patterns, focus):
        max_depth = min(depth + 1, 5)
        lines = ["\n## Directory Structure\n```"]
        displayed = 0
        display_limit = 300

        def walk(dir_path, current_depth, prefix=""):
            nonlocal displayed
            if current_depth > max_depth or displayed >= display_limit:
                return
            try:
                entries = sorted(dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except PermissionError:
                return
            for i, entry in enumerate(entries):
                if entry.name in _IGNORE_DIRS:
                    continue
                if entry.name.startswith(".") and entry.is_dir() and entry.name not in {".github", ".gitlab"}:
                    continue
                is_last = i == len(entries) - 1
                connector = "└── " if is_last else "├── "
                extension = "│   " if not is_last else "    "
                rel_name = entry.name
                if displayed >= display_limit:
                    lines[0] = lines[0].rstrip()
                    lines.append(f"{prefix}{connector}... ({display_limit}+ entries)")
                    return
                if entry.is_dir():
                    lines.append(f"{prefix}{connector}📁 {rel_name}/")
                    displayed += 1
                    walk(entry, current_depth + 1, prefix + extension)
                else:
                    ext = entry.suffix.lower()
                    icon = "📄"
                    if ext in {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb"}:
                        icon = "📝"
                    elif ext in {".md", ".rst", ".txt"}:
                        icon = "📖"
                    elif ext in {".json", ".yaml", ".yml", ".toml", ".cfg", ".ini"}:
                        icon = "⚙️"
                    elif ext in {".html", ".css", ".scss", ".vue", ".svelte"}:
                        icon = "🎨"
                    lines.append(f"{prefix}{connector}{icon} {rel_name}")
                    displayed += 1

        walk(root, 0)
        lines.append("```\n")
        if displayed >= display_limit:
            lines.append(f"*Structure truncated at {display_limit} entries. Use glob/read_file for specific paths.*\n")
        return "\n".join(lines)

    def _identify_key_files(self, root, include_patterns):
        categories = {}
        key_indicators = {
            "Entry Points": [
                "main.py", "main.ts", "index.js", "index.ts", "index.tsx",
                "app.py", "app.ts", "app.tsx", "server.py", "server.ts",
                "__main__.py", "cli.py", "start.js", "App.vue",
                "main.go", "main.rs", "main.java", "Program.cs",
                "run.py", "manage.py", "Procfile", "Dockerfile",
                "docker-compose.yml", "Makefile",
            ],
            "Config": [
                "pyproject.toml", "setup.py", "setup.cfg", "package.json",
                "tsconfig.json", "Cargo.toml", "pom.xml", "build.gradle",
                "go.mod", "composer.json", "Gemfile", "requirements.txt",
                ".env.example", "config.py", "config.ts", "settings.py",
                "application.yml", "application.properties",
            ],
            "Core Logic": [],
            "Tests": [],
            "Documentation": [],
        }

        found_entries = {cat: [] for cat in key_indicators}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in path.parts for p in _IGNORE_DIRS):
                continue
            rel = path.relative_to(root)
            name = path.name
            lower_name = name.lower()

            for cat, indicators in key_indicators.items():
                if any(lower_name == ind.lower() for ind in indicators):
                    found_entries[cat].append(path)
                    break

            parent_lower = path.parent.name.lower()
            if parent_lower in {"src", "lib", "core", "app", "internal", "pkg"}:
                if path.suffix in {".py", ".js", ".ts", ".go", ".rs", ".java"}:
                    found_entries["Core Logic"].append(path)
            if parent_lower in {"test", "tests", "__tests__", "spec", "specs"} or lower_name.startswith(("test_", "test-", "_test")):
                found_entries["Tests"].append(path)
            if path.suffix in {".md", ".rst"} and path.parent.name.lower() not in {"node_modules"}:
                found_entries["Documentation"].append(path)

        for cat, files in found_entries.items():
            if files:
                deduped = list(dict.fromkeys(files))
                categories[cat] = sorted(deduped, key=lambda f: f)[:10]

        core_deduped = list(dict.fromkeys(categories.get("Core Logic", [])))
        categories["Core Logic"] = sorted(core_deduped, key=lambda f: f.stat().st_size, reverse=True)[:15]
        return categories

    def _detect_architecture_patterns(self, root, lang_stats):
        patterns = []
        langs = set(lang_stats.keys())

        dirs_seen = set()
        for p in root.iterdir():
            if p.is_dir():
                dirs_seen.add(p.name.lower())

        if "python" in langs:
            for d in root.iterdir():
                if d.is_dir() and d.name.lower() in {"views", "templates", "urls"}:
                    patterns.append("MTV/MVC Pattern (Python web framework style)")
                    break

        if any(d in dirs_seen for d in {"src", "lib", "packages"}):
            patterns.append("Standard src/ layout")

        pattern_map = {
            ("controller", "controllers"): "MVC Controllers pattern",
            ("service", "services"): "Service Layer pattern",
            ("repository", "repositories", "repo"): "Repository Pattern",
            ("middleware", "middlewares"): "Middleware pattern",
            ("hook", "hooks"): "Hooks pattern (React/Vue-style or custom)",
            ("store", "stores"): "State Management / Store pattern",
            ("agent", "agents"): "Agent-based architecture",
            ("engine", "engines"): "Engine/Kernel pattern",
            ("provider", "providers"): "Provider/Strategy pattern",
            ("channel", "channels"): "Channel/Adapter pattern",
            ("bus",): "Event Bus / Message Bus pattern (dir: bus)",
            ("event", "events"): "Event Bus / Message Bus pattern (dir: events)",
            ("memory",): "Memory/Tiered Storage pattern",
            ("skill", "skills"): "Skill/Plugin system pattern",
            ("cron",): "Scheduled task / Cron pattern",
            ("mcp",): "MCP (Model Context Protocol) integration",
        }
        for dir_names, desc in pattern_map.items():
            if any(d in dirs_seen for d in dir_names):
                patterns.append(desc)

        if "javascript" in langs or "typescript" in langs:
            if any(d in dirs_seen for d in {"components", "pages", "hooks"}):
                patterns.append("Frontend component architecture (React/Next/Vue)")

        if any((root / fn).exists() for fn in ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"]):
            patterns.append("Containerized (Docker)")
        if (root / "pyproject.toml").exists():
            patterns.append("Python project (hatch/pyproject.toml)")
        if (root / "package.json").exists():
            patterns.append("Node.js/JavaScript project")

        return patterns[:15]

    def _find_config_files(self, root):
        configs = []
        config_names = {
            "pyproject.toml", "setup.py", "setup.cfg", "tox.ini",
            "package.json", "tsconfig.json", "webpack.config.js",
            "vite.config.ts", "next.config.js", "tailwind.config.js",
            "Cargo.toml", "Go.mod", "pom.xml", "build.gradle",
            "Makefile", "Dockerfile", "docker-compose.yml",
            ".eslintrc*", ".prettierrc*", ".babelrc*",
            "requirements.txt", "Pipfile", "poetry.lock",
            ".env.example", ".gitignore", ".editorconfig",
            "LICENSE", "README.md", "CHANGELOG.md",
            "jest.config.js", "vitest.config.ts", "pytest.ini",
            "conftest.py", ".github/workflows",
        }
        for name in config_names:
            if (root / name).exists():
                configs.append(root / name)
        for cf in root.glob("*.config.*"):
            if cf not in configs:
                configs.append(cf)
        return sorted(configs)[:20]

    def _find_entry_points(self, root, lang_stats, include_patterns):
        entries = []
        ep_names = {
            "__main__.py", "main.py", "main.ts", "main.js", "index.js",
            "index.ts", "index.tsx", "index.vue", "app.py", "app.ts",
            "app.tsx", "server.py", "server.ts", "cli.py", "run.py",
            "manage.py", "start.js", "Application.java", "Program.cs",
            "main.go", "main.rs", "lib.rs", "mod.rs",
        }
        for name in ep_names:
            candidate = root / name
            if candidate.exists() and candidate.is_file():
                entries.append(candidate)
        for d in ["src", "app"]:
            subdir = root / d
            if subdir.is_dir():
                for name in ["main.ts", "main.js", "index.ts", "index.js", "index.tsx", "App.tsx"]:
                    c = subdir / name
                    if c.exists():
                        entries.append(c)
        return list(dict.fromkeys(entries))[:15]

    def _get_file_summary(self, path):
        try:
            raw = path.read_bytes()
            size = len(raw)
            if b"\x00" in raw:
                return f"binary, {size} bytes"
            text = raw.decode("utf-8", errors="replace")
            lines = text.count("\n") + 1
            ext = path.suffix.lower()
            lang = _LANG_EXTENSIONS.get(ext, "text")
            return f"{lang}, {lines} lines, {size:,} bytes"
        except Exception:
            return "unreadable"

    def _get_entry_description(self, path):
        name = path.name.lower()
        descriptions = {
            "__main__.py": "Python package entry point",
            "main.py": "Main module",
            "main.ts/main.js": "Main entry point (JS/TS)",
            "index.ts/index.js": "Module index / re-export",
            "app.py/app.ts/app.tsx": "Application bootstrap",
            "server.py/server.ts": "Server startup",
            "cli.py": "CLI interface",
            "manage.py": "Management commands",
            "dockerfile": "Docker build definition",
        }
        for key, desc in descriptions.items():
            if name in key:
                return desc
        return "Entry point"

    def _suggest_next_steps(self, root, focus, key_files, patterns):
        suggestions = [
            "| Step | Action | Command |",
            "|------|--------|---------|",
        ]
        step = 1

        if key_files and "Entry Points" in key_files:
            for f in key_files["Entry Points"][:2]:
                rel = f.relative_to(root)
                suggestions.append(f"| {step} | Read entry point | `read_file(\"{rel}\")` |")
                step += 1

        if key_files and "Core Logic" in key_files:
            for f in key_files["Core Logic"][:2]:
                rel = f.relative_to(root)
                suggestions.append(f"| {step} | Analyze core module | `explore(mode=\"analyze\", target=\"{rel}\")` |")
                step += 1

        suggestions.append(f"| {step} | Trace key symbols | `explore(mode=\"trace\", target=\"<symbol_name>\")` |")
        step += 1
        suggestions.append(f"| {step} | View dependency graph | `explore(mode=\"dependencies\")` |")
        step += 1

        if focus:
            suggestions.append(f'| {step} | Focus search on "{focus}" | `grep(pattern="{focus}", include="*.py")` |')

        return "\n".join(suggestions) + "\n"

    def _find_symbol_definitions(self, root, symbol, include_patterns):
        results = []
        symbol_lower = symbol.lower()
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in path.parts for p in _IGNORE_DIRS):
                continue
            if include_patterns:
                if not any(path.match(p) for p in include_patterns):
                    continue
            ext = path.suffix.lower()
            if ext != ".py":
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(content, filename=str(path))
                for node in ast.walk(tree):
                    node_name = ""
                    kind = ""
                    sig = ""
                    if isinstance(node, ast.FunctionDef):
                        node_name = node.name
                        kind = "function"
                        sig = f"def {node.name}({self._ast_args(node)})"
                    elif isinstance(node, ast.AsyncFunctionDef):
                        node_name = node.name
                        kind = "async function"
                        sig = f"async def {node.name}({self._ast_args(node)})"
                    elif isinstance(node, ast.ClassDef):
                        node_name = node.name
                        kind = "class"
                        bases = ", ".join(self._ast_get_name(b) for b in node.bases)
                        sig = f"class {node.name}({bases})"
                    elif isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id == symbol:
                                kind = "variable"
                                node_name = target.id
                                sig = f"{target.id} = ..."
                                break

                    if node_name and node_name.lower() == symbol_lower:
                        results.append((path, node.lineno, kind, sig))
            except (SyntaxError, ValueError, UnicodeDecodeError):
                continue
        return results

    def _find_fuzzy_definitions(self, root, symbol, include_patterns):
        results = []
        symbol_lower = symbol.lower()
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in path.parts for p in _IGNORE_DIRS):
                continue
            if include_patterns:
                if not any(path.match(p) for p in include_patterns):
                    continue
            ext = path.suffix.lower()
            if ext != ".py":
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(content, filename=str(path))
                for node in ast.walk(tree):
                    node_name = ""
                    kind = ""
                    sig = ""
                    if isinstance(node, ast.FunctionDef):
                        node_name = node.name
                        kind = "func"
                        sig = f"def {node.name}({self._ast_args(node)})"
                    elif isinstance(node, ast.AsyncFunctionDef):
                        node_name = node.name
                        kind = "async func"
                        sig = f"async def {node.name}({self._ast_args(node)})"
                    elif isinstance(node, ast.ClassDef):
                        node_name = node.name
                        kind = "class"
                        bases = ", ".join(self._ast_get_name(b) for b in node.bases)
                        sig = f"class {node.name}({bases})"

                    if node_name:
                        ratio = self._fuzzy_match(symbol_lower, node_name.lower())
                        if ratio >= 0.4 and ratio < 1.0:
                            results.append((path, node.lineno, kind, sig, ratio))
            except (SyntaxError, ValueError, UnicodeDecodeError):
                continue
        results.sort(key=lambda x: -x[4])
        return results

    def _find_symbol_usages(self, root, symbol, include_patterns, limit_per_file=5):
        usages = {}
        escaped = re.escape(symbol)
        pattern = re.compile(r'\b' + escaped + r'\b')
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in path.parts for p in _IGNORE_DIRS):
                continue
            if include_patterns:
                if not any(path.match(p) for p in include_patterns):
                    continue
            ext = path.suffix.lower()
            if ext not in {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb"}:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                lines = content.splitlines()
                file_refs = []
                for i, line in enumerate(lines, 1):
                    if pattern.search(line) and len(file_refs) < limit_per_file:
                        file_refs.append((i, line))
                if file_refs:
                    usages[path] = file_refs
            except (UnicodeDecodeError, PermissionError):
                continue
        return usages

    def _find_imports_of_symbol(self, root, symbol, include_patterns):
        imports = []
        escaped = re.escape(symbol)
        imp_pattern = re.compile(r'^\s*(?:from|import)\s+.*\b' + escaped + r'\b', re.MULTILINE)
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in path.parts for p in _IGNORE_DIRS):
                continue
            if include_patterns:
                if not any(path.match(p) for p in include_patterns):
                    continue
            ext = path.suffix.lower()
            if ext not in {".py", ".js", ".ts", ".tsx", ".jsx"}:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                match = imp_pattern.search(content)
                if match:
                    imports.append((path, match.group(0).strip()))
            except (UnicodeDecodeError, PermissionError):
                continue
        return imports

    def _trace_call_chain(self, root, symbol, definitions, usages, include_patterns, max_depth=3):
        if not definitions and not usages:
            return ""

        lines = ["```mermaid"]
        nodes = set()
        edges = []

        def add_node(name, kind="symbol"):
            safe = name.replace('"', '').replace('(', '_').replace(')', '_')[:30]
            label = name[:25]
            if kind == "definition":
                nodes.add(f'{safe}[("{label}"):::def]')
            elif kind == "usage":
                nodes.add(f'{safe}[("{label}"):::use]')
            else:
                nodes.add(f'{safe}["{label}"]')
            return safe

        if definitions:
            for filepath, line_no, kind, sig in definitions[:5]:
                rel = filepath.relative_to(root)
                label = f"{symbol}\n({rel.name}:{line_no})"
                nid = add_node(label, "definition")
                for upath, refs in list(usages.items())[:10]:
                    urel = upath.relative_to(root)
                    uid = add_node(f"{urel}", "usage")
                    edges.append(f'{uid} --> {nid}')

        if edges:
            lines.append("graph LR")
            for n in list(nodes)[:30]:
                lines.append(f"  {n}")
            for e in edges[:40]:
                lines.append(f"  {e}")
            lines.append("```")
            return "\n".join(lines)
        return ""

    def _find_related_symbols(self, root, symbol, definitions, usages, include_patterns):
        related = []
        seen = set()

        if definitions:
            for filepath, _, _, _ in definitions[:3]:
                try:
                    content = filepath.read_text(encoding="utf-8", errors="ignore")
                    tree = ast.parse(content, filename=str(filepath))
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            name = node.name
                            if name.lower() != symbol.lower() and name not in seen:
                                related.append((name, f"defined in same file as '{symbol}'"))
                                seen.add(name)
                        elif isinstance(node, ast.ClassDef):
                            name = node.name
                            if name.lower() != symbol.lower() and name not in seen:
                                related.append((name, f"class in same file as '{symbol}'"))
                                seen.add(name)
                except (SyntaxError, UnicodeDecodeError):
                    continue

        for filepath, refs in list(usages.items())[:5]:
            try:
                content = filepath.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(content, filename=str(filepath))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        name = node.name
                        if name not in seen:
                            related.append((name, f"caller/user of '{symbol}' in {filepath.name}"))
                            seen.add(name)
                    elif isinstance(node, ast.ClassDef):
                        name = node.name
                        if name not in seen:
                            related.append((name, f"containing class where '{symbol}' is used"))
                            seen.add(name)
            except (SyntaxError, UnicodeDecodeError):
                continue

        return related[:20]

    def _analyze_single_file(self, file_path, base_path, depth, focus):
        sections = [f"# Deep Analysis: `{file_path.relative_to(base_path)}`\n"]

        try:
            raw = file_path.read_bytes()
            if b"\x00" in raw:
                return [f"Error: Binary file cannot be analyzed: {file_path}"]
            content = raw.decode("utf-8", errors="replace")
        except Exception as e:
            return [f"Error reading file: {e}"]

        lines_list = content.splitlines()
        total_lines = len(lines_list)
        size = len(raw)
        ext = file_path.suffix.lower()
        lang = _LANG_EXTENSIONS.get(ext, "text")

        sections.append(f"**Language**: {lang} | **Lines**: {total_lines} | **Size**: {size:,} bytes\n")

        head_count = min(total_lines, 80)
        numbered = [f"{i + 1:>4}| {line}" for i, line in enumerate(lines_list[:head_count])]
        sections.append("## File Head (first 80 lines)\n```")
        sections.extend(numbered)
        sections.append("```\n")

        if total_lines > 80:
            tail_start = max(81, total_lines - 30)
            tail_lines = [f"{i + 1:>4}| {line}" for i, line in enumerate(lines_list[tail_start:])]
            sections.append(f"## File Tail (lines {tail_start}-{total_lines})\n```")
            sections.extend(tail_lines)
            sections.append("```\n")

        if ext == ".py":
            sections.extend(self._python_analysis(file_path, content, lines_list, base_path, depth))

        imports_section = self._extract_imports(file_path, content, base_path)
        if imports_section:
            sections.append(imports_section)

        related_files = self._find_related_files(file_path, base_path, depth)
        if related_files:
            rl = ["\n## Related Files (referenced or referencing this file)\n"]
            for rel_path, relation in related_files[:15]:
                rl.append(f"- `{rel_path}` — {relation}")
            sections.append("\n".join(rl))

        return sections

    def _analyze_directory(self, dir_path, base_path, depth, focus, include_patterns):
        sections = [f"# Directory Analysis: `{dir_path.relative_to(base_path)}`\n"]

        all_files = []
        for p in dir_path.rglob("*"):
            if not p.is_file():
                continue
            if any(x in p.parts for x in _IGNORE_DIRS):
                continue
            if include_patterns:
                if not any(p.match(pat) for pat in include_patterns):
                    continue
            all_files.append(p)

        all_files.sort(key=lambda f: f.stat().st_size, reverse=True)
        sections.append(f"Contains **{len(all_files)}** files.\n")

        fl = ["## Files by Size (largest first)\n"]
        for f in all_files[:25]:
            rel = f.relative_to(base_path)
            summary = self._get_file_summary(f)
            fl.append(f"- `{rel}` ({summary})")
        sections.append("\n".join(fl))

        subdirs = [d for d in dir_path.iterdir() if d.is_dir() and d.name not in _IGNORE_DIRS]
        if subdirs:
            sdl = ["\n## Subdirectories\n"]
            for sd in sorted(subdirs, key=lambda d: -len(list(sd.rglob("*"))))[:15]:
                count = len([p for p in sd.rglob("*") if p.is_file()])
                rel_sd = sd.relative_to(base_path)
                sdl.append(f"- `{rel_sd}/` ({count} files)")
            sections.append("\n".join(sdl))

        if all_files:
            largest = all_files[0]
            if largest.suffix.lower() == ".py":
                rel_largest = largest.relative_to(base_path)
                sections.append(f"\n> 💡 Run `explore(mode='analyze', target='{rel_largest}')` for deep analysis of the largest file.")

        return sections

    def _python_analysis(self, file_path, content, lines_list, base_path, depth):
        sections = []
        try:
            tree = ast.parse(content, filename=str(file_path))
        except SyntaxError:
            sections.append("> ⚠️ File has syntax errors, skipping AST analysis.\n")
            return sections

        classes = []
        functions = []
        async_functions = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                bases = [self._ast_get_name(b) for b in node.bases]
                methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
                props = sum(1 for n in node.body if isinstance(n, (ast.Assign, ast.AnnAssign)))
                decos = [self._ast_get_deco_name(d) for d in node.decorator_list]
                classes.append({
                    "name": node.name,
                    "line": node.lineno,
                    "bases": bases,
                    "methods": methods,
                    "properties": props,
                    "decorators": decos,
                })
            elif isinstance(node, ast.FunctionDef):
                functions.append({"name": node.name, "line": node.lineno, "args": len(node.args.args)})
            elif isinstance(node, ast.AsyncFunctionDef):
                async_functions.append({"name": node.name, "line": node.lineno})

        if classes:
            clines = ["\n## Classes\n"]
            for cls in classes:
                bases_str = f"({', '.join(cls['bases'])})" if cls["bases"] else ""
                methods_preview = ", ".join(cls["methods"][:8])
                if len(cls["methods"]) > 8:
                    methods_preview += f" (+{len(cls['methods']) - 8} more)"
                decos_str = f' [{", ".join(cls["decorators"])}]' if cls["decorators"] else ""
                clines.append(
                    f'- **`{cls["name"]}{bases_str}`** L{cls["line"]} '
                    f'— {len(cls["methods"])} methods: {{{methods_preview}}}{decos_str}'
                )
            sections.append("\n".join(clines))

        if functions:
            fl = ["\n## Functions\n"]
            for func in functions[:20]:
                fl.append(f"- **`{func['name']}()`** L{func['line']} ({func['args']} params)")
            if len(functions) > 20:
                fl.append(f"- ... and {len(functions) - 20} more functions")
            sections.append("\n".join(fl))

        if async_functions:
            al = ["\n## Async Functions\n"]
            for af in async_functions[:10]:
                al.append(f"- **`async {af['name']}()`** L{af['line']}")
            sections.append("\n".join(al))

        all_top_level = len(classes) + len(functions) + len(async_functions)
        sections.append(f"\n**Summary**: {len(classes)} classes, {len(functions)} functions, {len(async_functions)} async functions ({all_top_level} top-level definitions).\n")

        return sections

    def _extract_imports(self, file_path, content, base_path):
        ext = file_path.suffix.lower()
        if ext not in {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs"}:
            return ""

        imports = []
        if ext == ".py":
            try:
                tree = ast.parse(content, filename=str(file_path))
                for node in ast.iter_child_nodes(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imports.append(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        names = ", ".join(
                            (a.name + (f" as {a.asname}" if a.asname else "")) for a in node.names
                        )
                        level = "." * node.level
                        imports.append(f"from {level}{module} import {names}")
            except SyntaxError:
                pass
        else:
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ", "require(", "const ")) and len(stripped) < 200:
                    imports.append(stripped)

        if not imports:
            return ""
        il = [f"\n## Imports ({len(imports)})\n"]
        for imp in imports[:30]:
            il.append(f"- {imp}")
        if len(imports) > 30:
            il.append(f"- ... and {len(imports) - 30} more imports")
        return "\n".join(il) + "\n"

    def _find_related_files(self, file_path, base_path, depth):
        related = []
        stem = file_path.stem

        parent = file_path.parent
        for sibling in parent.iterdir():
            if sibling.is_file() and sibling != file_path and sibling.suffix == file_path.suffix:
                rel_sib = sibling.relative_to(base_path)
                related.append((str(rel_sib), "sibling in same directory"))

        test_patterns = [
            f"test_{stem}.py", f"{stem}_test.py", f"test_{stem}.ts",
            f"{stem}.test.ts", f"{stem}.spec.ts", f"__tests__/{stem}.ts",
        ]
        for tp in test_patterns:
            candidate = parent / tp
            if candidate.exists():
                rel = candidate.relative_to(base_path)
                related.append((str(rel), "test file"))

        init_file = parent / "__init__.py"
        if init_file.exists():
            rel = init_file.relative_to(base_path)
            related.append((str(rel), "package init"))

        return related

    def _build_dependency_graph(self, root, include_patterns, depth):
        deps = self._collect_internal_deps(root, include_patterns)

        if not deps:
            return ""

        lines = ["## Internal Module Dependencies\n"]
        lines.append("| Module | Depends On |")
        lines.append("|--------|-----------|")
        for mod, depends_on in sorted(deps.items()):
            internal = [d for d in depends_on if self._is_local_module(root, d)]
            if internal:
                lines.append(f"| `{mod}` | {', '.join(f'`{m}`' for m in internal[:8])} |")

        if len(deps) > 20:
            lines.append(f"\n*Showing {min(len(deps), 20)} of {len(deps)} modules.*")

        return "\n".join(lines) + "\n"

    def _is_local_module(self, root, module_name):
        module_path = module_name.replace(".", "/")
        candidates = [
            root / module_path,
            root / module_path / "__init__.py",
            root / (module_path + ".py"),
        ]
        return any(c.exists() for c in candidates)

    def _collect_internal_deps(self, root, include_patterns):
        """Collect internal module dependencies as a graph.

        Returns dict mapping module path (str) -> set of imported local
        module names. Reused by dependency analysis helpers.
        """
        stdlib_modules = {
            "os", "sys", "typing", "dataclasses", "enum", "collections",
            "functools", "itertools", "contextlib", "abc", "io", "re", "json",
            "pathlib", "datetime", "logging", "asyncio", "threading",
            "multiprocessing", "unittest", "pytest", "unittest.mock", "__future__",
            "warnings", "copy", "math", "random", "hashlib", "time", "tempfile",
            "subprocess", "textwrap", "string", "struct", "array", "bisect",
            "heapq", "operator", "pprint", "reprlib", "numbers", "decimal",
            "fractions", "types", "inspect", "dis", "pickle", "shelve",
            "sqlite3", "csv", "configparser", "argparse", "getopt", "errno",
            "ctypes", "socket", "ssl", "select", "signal", "email", "html",
            "xml", "xmlrpc", "urllib", "http", "ftplib", "poplib", "imaplib",
            "smtplib", "nntplib", "uuid", "base64", "binascii", "quopri",
            "uu", "codecs", "mimetypes",
        }
        deps: dict[str, set[str]] = {}

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in path.parts for p in _IGNORE_DIRS):
                continue
            if include_patterns:
                if not any(path.match(p) for p in include_patterns):
                    continue
            if path.suffix.lower() != ".py":
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(content, filename=str(path))
                imported_modules = set()
                for node in ast.iter_child_nodes(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        mod_parts = node.module.split(".")
                        if mod_parts[0] not in stdlib_modules:
                            imported_modules.add(mod_parts[0])
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            top = alias.name.split(".")[0]
                            if top not in stdlib_modules:
                                imported_modules.add(top)
                if imported_modules:
                    rel = path.relative_to(root)
                    deps[str(rel)] = imported_modules
            except (SyntaxError, UnicodeDecodeError):
                continue
        return deps

    def _module_path_to_name(self, module_str: str) -> str:
        """Convert a file path like 'pkg/mod.py' or 'pkg/mod/__init__.py' to a module name 'pkg.mod'."""
        cleaned = module_str.replace("\\", "/")
        if cleaned.endswith("/__init__.py"):
            cleaned = cleaned[: -len("/__init__.py")]
        elif cleaned.endswith(".py"):
            cleaned = cleaned[: -len(".py")]
        return cleaned.replace("/", ".")

    def _detect_circular_deps(self, root, include_patterns):
        """Detect circular dependencies among internal modules via DFS."""
        deps = self._collect_internal_deps(root, include_patterns)
        if not deps:
            return []

        # Build adjacency: module name -> set of module names it depends on
        graph: dict[str, set[str]] = {}
        for mod_path, imported in deps.items():
            mod_name = self._module_path_to_name(mod_path)
            graph.setdefault(mod_name, set())
            for dep in imported:
                if self._is_local_module(root, dep):
                    graph[mod_name].add(dep)

        cycles: list[list[str]] = []
        seen_cycles: set[tuple[str, ...]] = set()

        def dfs(node: str, path: list[str], visited: set[str]):
            if node in path:
                idx = path.index(node)
                cycle = path[idx:] + [node]
                key = tuple(cycle)
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append(cycle)
                return
            if node in visited:
                return
            visited.add(node)
            for neighbor in graph.get(node, ()):
                dfs(neighbor, path + [node], visited)

        for start in list(graph.keys()):
            dfs(start, [], set())

        return cycles

    def _identify_core_modules(self, root, include_patterns):
        """Identify core modules by connectivity (in-degree + out-degree)."""
        deps = self._collect_internal_deps(root, include_patterns)
        if not deps:
            return []

        scores: dict[str, int] = {}
        for mod_path, imported in deps.items():
            mod_name = self._module_path_to_name(mod_path)
            scores[mod_name] = scores.get(mod_name, 0) + len(imported)
            for dep in imported:
                if self._is_local_module(root, dep):
                    scores[dep] = scores.get(dep, 0) + 1

        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return [(name, score) for name, score in ranked if score > 0]

    def _find_orphan_files(self, root, include_patterns):
        """Find Python files that are neither imported by nor import any local module."""
        deps = self._collect_internal_deps(root, include_patterns)
        if not deps:
            return []

        imported_set: set[str] = set()
        importers_set: set[str] = set(deps.keys())

        for mod_path, imported in deps.items():
            for dep in imported:
                if self._is_local_module(root, dep):
                    imported_set.add(dep)

        orphans = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in path.parts for p in _IGNORE_DIRS):
                continue
            if include_patterns:
                if not any(path.match(p) for p in include_patterns):
                    continue
            if path.suffix.lower() != ".py":
                continue
            rel = path.relative_to(root)
            mod_name = self._module_path_to_name(str(rel))
            # A file is orphan if it doesn't import any local module AND
            # no local module imports it.
            if mod_name not in importers_set and mod_name not in imported_set:
                orphans.append(path)

        return orphans

    @staticmethod
    def _ast_args(func_node):
        args = []
        defaults_dict = {}
        defaults_list = list(func_node.args.defaults)
        all_args = func_node.args.args
        offset = len(all_args) - len(defaults_list)
        for i, arg in enumerate(all_args):
            annotation = ""
            if arg.annotation:
                try:
                    annotation = f": {ast.unparse(arg.annotation)}"
                except Exception:
                    pass
            default = ""
            di = i - offset
            if 0 <= di < len(defaults_list):
                default = f" = {ast.unparse(defaults_list[di])}"
            args.append(f"{arg.arg}{annotation}{default}")
        if func_node.args.vararg:
            args.append(f"*{func_node.args.vararg.arg}")
        if func_node.args.kwarg:
            args.append(f"**{func_node.args.kwarg.arg}")
        return ", ".join(args)

    @staticmethod
    def _ast_get_name(node):
        try:
            return ast.unparse(node)
        except Exception:
            return "?"

    @staticmethod
    def _ast_get_deco_name(node):
        try:
            return ast.unparse(node.func) if hasattr(node, "func") else ast.unparse(node)
        except Exception:
            return "?"

    @staticmethod
    def _fuzzy_match(a, b):
        if a == b:
            return 1.0
        if a in b:
            return 0.7 + (0.2 * len(a) / len(b))
        if b in a:
            return 0.6 + (0.2 * len(b) / len(a))
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio()
