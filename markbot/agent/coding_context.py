"""Coding context detector — one-time project facts预热.

Scans the workspace for manifests, package managers, and verify commands
(test / lint / type-check / build) and renders them as a stable prompt
section. The section is baked into the system prompt so the model knows
*up front* how to verify its work in this project, instead of having to
discover `pytest` / `npm test` / `make test` by trial and error.

Mirrors agent's ``coding_context.detect_project_facts``: byte-stable so
it lands in the prompt cache prefix, and the verify commands are written
to the model itself so it internalises "done = edited + verified"
without per-prompt enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class ProjectFacts:
    """Detected facts about a workspace relevant to coding tasks."""

    language: str = ""
    package_manager: str = ""
    manifests: list[str] = field(default_factory=list)
    verify_commands: list[str] = field(default_factory=list)
    lint_commands: list[str] = field(default_factory=list)
    typecheck_commands: list[str] = field(default_factory=list)
    context_files: list[str] = field(default_factory=list)

    def has_any(self) -> bool:
        return bool(
            self.language
            or self.package_manager
            or self.verify_commands
            or self.lint_commands
            or self.typecheck_commands
            or self.context_files
        )


# Manifests we recognise, in priority order. The first match wins for
# language / package_manager attribution.
_MANIFESTS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    # (filename, language, package_manager, verify_commands)
    ("pyproject.toml", "Python", "pip", ("pytest", "python -m pytest")),
    ("setup.py", "Python", "pip", ("pytest", "python -m pytest")),
    ("requirements.txt", "Python", "pip", ("pytest", "python -m pytest")),
    ("package.json", "JavaScript/TypeScript", "npm", ("npm test",)),
    ("pnpm-workspace.yaml", "JavaScript/TypeScript", "pnpm", ("pnpm test",)),
    ("Cargo.toml", "Rust", "cargo", ("cargo test",)),
    ("go.mod", "Go", "go", ("go test ./...",)),
    ("pom.xml", "Java (Maven)", "maven", ("mvn test",)),
    ("build.gradle", "Java (Gradle)", "gradle", ("./gradlew test",)),
    ("build.gradle.kts", "Java (Gradle)", "gradle", ("./gradlew test",)),
    ("Gemfile", "Ruby", "bundler", ("bundle exec rake test",)),
    ("mix.exs", "Elixir", "mix", ("mix test",)),
    ("composer.json", "PHP", "composer", ("composer test",)),
)

# Makefile targets that count as verify commands.
_MAKEFILE_VERIFY_TARGETS = ("test", "check", "verify", "lint", "typecheck")

# Conventional context files that the model should read before editing.
_CONTEXT_FILE_NAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "README.md",
)


def detect_project_facts(workspace: Path) -> ProjectFacts:
    """One-time scan of the workspace for project facts.

    Cheap filesystem checks only — no subprocess calls, no parsing of
    manifest contents beyond a quick grep for the ``scripts.test`` /
    ``[tool.pytest]`` keys. Safe to call on every system-prompt rebuild;
    callers should cache the result (``ContextBuilder`` does this via its
    own ``_context_cache``).
    """
    facts = ProjectFacts()
    if not workspace or not workspace.exists():
        return facts

    try:
        workspace = workspace.resolve()
    except Exception:
        pass

    # 1. Detect language + package manager + default verify commands
    for filename, lang, pm, verify in _MANIFESTS:
        if (workspace / filename).exists():
            facts.language = lang
            facts.package_manager = pm
            facts.manifests.append(filename)
            facts.verify_commands.extend(verify)
            # For package.json, try to extract the actual test script so
            # we suggest the right command (e.g. "npm run test:unit" if
            # that's what the project uses).
            if filename == "package.json":
                _enrich_from_package_json(workspace, facts)
            # For pyproject.toml, check for pytest config so we know
            # pytest is the test runner (vs unittest).
            if filename in ("pyproject.toml", "setup.py"):
                _enrich_from_pyproject(workspace, facts)
            break  # first manifest wins

    # 2. Makefile — add make targets if present and not already covered
    makefile = workspace / "Makefile"
    if makefile.exists():
        facts.manifests.append("Makefile")
        try:
            text = makefile.read_text(encoding="utf-8", errors="replace")
            for target in _MAKEFILE_VERIFY_TARGETS:
                # Look for "target:" at line start (not "target-foo:").
                if any(
                    line.startswith(f"{target}:") or line.startswith(f".PHONY: {target}")
                    for line in text.splitlines()
                ):
                    cmd = f"make {target}"
                    if cmd not in facts.verify_commands:
                        facts.verify_commands.append(cmd)
        except Exception as exc:
            logger.debug("Failed to read Makefile: {}", exc)

    # 3. Conventional context files
    for name in _CONTEXT_FILE_NAMES:
        if (workspace / name).exists():
            facts.context_files.append(name)

    # 4. Lint / typecheck heuristics
    if facts.language == "Python":
        if (workspace / ".ruff.toml").exists() or (workspace / "ruff.toml").exists():
            facts.lint_commands.append("ruff check .")
        if (workspace / ".flake8").exists():
            facts.lint_commands.append("flake8 .")
        # mypy.ini is a standalone config; [tool.mypy] in pyproject.toml is
        # already handled by _enrich_from_pyproject. Check both but guard
        # against duplicates since _enrich_from_pyproject may have run first.
        if (workspace / "mypy.ini").exists() and "mypy ." not in facts.typecheck_commands:
            facts.typecheck_commands.append("mypy .")
    elif facts.language == "JavaScript/TypeScript":
        if (workspace / ".eslintrc.json").exists() or (workspace / ".eslintrc.js").exists():
            facts.lint_commands.append("npm run lint")
        if (workspace / "tsconfig.json").exists():
            facts.typecheck_commands.append("npx tsc --noEmit")

    return facts


def _enrich_from_package_json(workspace: Path, facts: ProjectFacts) -> None:
    """Extract the actual test script from package.json if present."""
    try:
        import json

        data = json.loads((workspace / "package.json").read_text(encoding="utf-8"))
        scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
        test_script = scripts.get("test", "")
        # If the test script is a placeholder (e.g. "echo no test specified"),
        # remove the default `npm test` suggestion that _MANIFESTS added —
        # running it would just print noise, not verify anything.
        if test_script and _is_placeholder_test_script(test_script):
            try:
                facts.verify_commands.remove("npm test")
            except ValueError:
                pass
        # Surface lint / typecheck scripts if present.
        if scripts.get("lint"):
            facts.lint_commands.append("npm run lint")
        if scripts.get("typecheck") or scripts.get("type-check"):
            facts.typecheck_commands.append("npm run typecheck")
    except Exception as exc:
        logger.debug("Failed to parse package.json: {}", exc)


def _enrich_from_pyproject(workspace: Path, facts: ProjectFacts) -> None:
    """Detect pytest / ruff / mypy config in pyproject.toml."""
    pyproject = workspace / "pyproject.toml"
    if not pyproject.exists():
        return
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if "[tool.pytest" in text or "pytest" in text.lower():
            # pytest already in verify_commands from _MANIFESTS default
            pass
        if "[tool.ruff" in text:
            facts.lint_commands.append("ruff check .")
        if "[tool.mypy" in text:
            facts.typecheck_commands.append("mypy .")
    except Exception as exc:
        logger.debug("Failed to read pyproject.toml: {}", exc)


def _has_pyproject_section(workspace: Path, section: str) -> bool:
    pyproject = workspace / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        return f"[tool.{section}]" in text
    except Exception:
        return False


# Patterns that indicate a package.json "test" script is a placeholder,
# not a real test runner. Matching is case-insensitive on the lowercased
# script text.
_PLACEHOLDER_TEST_PATTERNS: tuple[str, ...] = (
    "echo no test",
    "echo \"no test",
    "echo 'no test",
    "no test specified",
    "exit 0",
)


def _is_placeholder_test_script(script: str) -> bool:
    """Return True if a package.json test script is a placeholder, not real."""
    lower = script.strip().lower()
    if not lower:
        return True
    return any(p in lower for p in _PLACEHOLDER_TEST_PATTERNS)


def render_coding_context_section(facts: ProjectFacts) -> str:
    """Render ProjectFacts as a system-prompt section.

    Byte-stable: the same facts always produce the same text, so this
    section lands in the prompt cache prefix. The model sees the verify
    commands on every turn without re-discovering them.
    """
    if not facts.has_any():
        return ""
    lines = ["# Coding Context (project facts)"]
    if facts.language:
        lines.append(f"- Language: {facts.language}")
    if facts.package_manager:
        lines.append(f"- Package manager: {facts.package_manager}")
    if facts.manifests:
        lines.append(f"- Manifests: {', '.join(facts.manifests)}")
    if facts.verify_commands:
        lines.append(f"- Verify (run before declaring done): {'; '.join(facts.verify_commands)}")
    if facts.lint_commands:
        lines.append(f"- Lint: {'; '.join(facts.lint_commands)}")
    if facts.typecheck_commands:
        lines.append(f"- Typecheck: {'; '.join(facts.typecheck_commands)}")
    if facts.context_files:
        lines.append(f"- Context files: {', '.join(facts.context_files)}")
    lines.append("")
    lines.append(
        "When you edit code in this project, run the Verify command before "
        "declaring the task done. Completion = edited + verified."
    )
    return "\n".join(lines)
